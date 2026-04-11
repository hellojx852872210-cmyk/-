# -*- coding: utf-8 -*-
"""
自动化任务实现
每个任务函数由 scheduler 定期调用，或通过企业微信指令触发
"""
from __future__ import annotations

import copy
import datetime
import logging
from typing import Callable, Optional

from ..config import cfg
from ..core.batch_store import BatchItemStore
from ..core.models import BatchItem, PriceChangeRecord, PriceTrigger, ProductStatus
from ..core.price_history import get_history_db
from ..core.pricing_engine import PricingEngine
from ..core.rule_engine import RuleEngine
from ..services.data_store import AccountStore, CostPriceMap, SoldCache
from ..services.erp_service import ErpConfig, ErpFetcher
from ..services.zhuanzhuan_api import DataFetcher, ImeiService

logger = logging.getLogger(__name__)


def _log(msg: str, on_progress: Optional[Callable[[str], None]] = None) -> None:
    logger.info(msg)
    if on_progress:
        on_progress(msg)


def _refresh_imported_detail(svc: ImeiService, item: BatchItem):
    detail = None
    qc_code = str(item.qc_code or "").strip()
    if qc_code:
        detail = svc.query_by_qc_code(qc_code)
    if detail is None:
        imei = str(getattr(item, "imei", "") or "").strip()
        if imei:
            detail = svc.query_by_imei(imei)
    if detail is None and item.product_id:
        try:
            detail = svc._query_product_by_id(str(item.product_id), statuses=("0", "60", "80", "1"))
        except Exception:
            detail = None
    return detail


def _estimate_suggested_settle_price(svc: ImeiService, product_id: str, price: float) -> Optional[float]:
    if not product_id or price <= 0:
        return None
    try:
        return svc.estimate_settle_price(product_id, price)
    except Exception:
        return None


def _status_detail_from_obj(obj) -> str:
    for value in (
        getattr(obj, "status_detail", ""),
        getattr(obj, "status_text", ""),
        getattr(obj, "op_message", ""),
        getattr(obj, "reprice_msg", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    status = getattr(obj, "status", None)
    label = str(getattr(status, "label", "") or "").strip()
    return label


def _iter_actionable_items(imported_store: BatchItemStore) -> list[BatchItem]:
    items = imported_store.get_all()
    return [
        item for item in items
        if not getattr(item, "ignored", False) and getattr(item, "selected", True)
    ]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _get_custom_reprice_config() -> dict:
    mode = str(getattr(cfg, "auto_reprice_custom_offset_mode", "off") or "off").strip().lower()
    if mode not in {"off", "fixed", "percent"}:
        mode = "off"
    value = _safe_float(getattr(cfg, "auto_reprice_custom_offset_value", 0.0), 0.0)
    return {
        "mode": mode,
        "value": value,
        "enabled": mode != "off" and abs(value) > 0,
    }


def _describe_custom_reprice_offset(mode: str, value: float) -> str:
    if mode == "fixed":
        return f"固定 {value:+.0f} 元"
    if mode == "percent":
        return f"比例 {value:+.1f}%"
    return "关闭"


def _apply_custom_reprice_offset(price: Optional[float], mode: str, value: float) -> Optional[float]:
    if price is None:
        return None
    if mode == "fixed":
        adjusted = price + value
    elif mode == "percent":
        adjusted = price * (1 + value / 100)
    else:
        adjusted = price
    return max(round(adjusted, 0), 0)


def _min_target_profit(cost_price: float) -> float:
    if cost_price <= 0:
        return 0.0
    if cost_price < 1000:
        return 40.0
    if cost_price < 3000:
        return max(cost_price * 0.03, 60.0)
    return max(cost_price * 0.025, 100.0)


def _cost_floor_price(cost_price: float) -> Optional[float]:
    if cost_price <= 0:
        return None
    settle_target = cost_price + _min_target_profit(cost_price)
    target_price = PricingEngine.calc_price_from_settle_target(settle_target)
    if target_price is None:
        return None
    return round(target_price, 0)


def _age_discount_factor(days_on_sale: int) -> float:
    days = max(int(days_on_sale or 0), 0)
    if days <= 3:
        return 1.0
    if days <= 15:
        return max(1.0 - (days - 3) * 0.003, 0.85)
    return max(1.0 - (12 * 0.003) - (days - 15) * 0.005, 0.7)


def _clamp_price(price: Optional[float], floor_price: Optional[float], cap_price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    value = float(price)
    if floor_price is not None:
        value = max(value, floor_price)
    if cap_price is not None:
        value = min(value, cap_price)
    return round(value, 0)


def _assess_manual_review(item: Optional[BatchItem], price: Optional[float], cost_floor: Optional[float]) -> dict:
    settle_price = PricingEngine.calc_settle_price(price) if price is not None else None
    cost_price = _safe_float(getattr(item, "cost_price", 0.0) if item is not None else 0.0, 0.0)
    min_profit = _min_target_profit(cost_price) if cost_price > 0 else 0.0
    target_settle = cost_price + min_profit if cost_price > 0 else None
    below_cost = bool(cost_price > 0 and settle_price is not None and settle_price < cost_price)
    below_target_profit = bool(target_settle is not None and settle_price is not None and settle_price < target_settle)
    loss_amount = round(cost_price - settle_price, 0) if below_cost and settle_price is not None else 0.0
    target_profit_gap = round(target_settle - settle_price, 0) if below_target_profit and target_settle is not None and settle_price is not None else 0.0
    price_gap_to_cost_floor = round(cost_floor - price, 0) if cost_floor is not None and price is not None and price < cost_floor else 0.0

    reason = ""
    if below_cost and settle_price is not None:
        reason = f"建议价预计到手 {settle_price:.0f} 低于成本 {cost_price:.0f}，需人工确认"
    elif below_target_profit and settle_price is not None and target_settle is not None:
        reason = f"建议价预计到手 {settle_price:.0f} 低于目标到手 {target_settle:.0f}"

    return {
        "settle_price": settle_price,
        "cost_price": cost_price,
        "target_settle": target_settle,
        "below_cost": below_cost,
        "below_target_profit": below_target_profit,
        "needs_manual_review": below_cost,
        "loss_amount": loss_amount,
        "target_profit_gap": target_profit_gap,
        "price_gap_to_cost_floor": price_gap_to_cost_floor,
        "manual_review_reason": reason,
    }


def build_reprice_decision(
    item: Optional[BatchItem],
    pricing,
    rule_engine: Optional[RuleEngine] = None,
    *,
    custom_mode: Optional[str] = None,
    custom_value: Optional[float] = None,
    apply_rules: bool = False,
) -> dict:
    config = _get_custom_reprice_config()
    mode = str(custom_mode if custom_mode is not None else config["mode"] or "off").strip().lower()
    if mode not in {"off", "fixed", "percent"}:
        mode = "off"
    value = _safe_float(custom_value if custom_value is not None else config["value"], 0.0)
    custom_enabled = mode != "off" and abs(value) > 0

    system_price = None
    market_floor = None
    market_cap = None
    cost_floor = None
    age_discount_factor = 1.0
    age_adjusted_price = None
    market_base_price = None
    working_pricing = copy.deepcopy(pricing) if pricing is not None else None
    if working_pricing is not None:
        market_base_price = working_pricing.market_base_price
        market_cap = working_pricing.market_cap_price
        market_floor = working_pricing.market_floor_price or working_pricing.floor_price
        if item is not None:
            age_discount_factor = _age_discount_factor(item.days_on_sale)
            if market_base_price is not None:
                age_adjusted_price = round(market_base_price * age_discount_factor, 0)
            cost_floor = _cost_floor_price(float(getattr(item, "cost_price", 0.0) or 0.0))
        working_pricing.cost_floor_price = cost_floor
        working_pricing.age_discount_factor = age_discount_factor
        working_pricing.age_adjusted_price = age_adjusted_price
        if market_base_price is not None:
            working_pricing.market_base_price = market_base_price
        base_candidate = age_adjusted_price if age_adjusted_price is not None else working_pricing.suggest_price()
        base_candidate = _clamp_price(base_candidate, market_floor, market_cap)
        working_pricing.pre_rule_price = base_candidate
        working_pricing.recommended_price = base_candidate
        working_pricing.settle_price = PricingEngine.calc_settle_price(base_candidate) if base_candidate is not None else None
        if apply_rules and rule_engine is not None and item is not None:
            system_price = rule_engine.apply(item, working_pricing) or working_pricing.suggest_price()
        else:
            system_price = working_pricing.suggest_price()
    system_price = _clamp_price(system_price, market_floor, market_cap)
    if working_pricing is not None:
        working_pricing.recommended_price = system_price
        working_pricing.settle_price = PricingEngine.calc_settle_price(system_price) if system_price is not None else None

    final_price = _apply_custom_reprice_offset(system_price, mode, value)
    final_price = _clamp_price(final_price, market_floor, market_cap)
    manual_review = _assess_manual_review(item, final_price, cost_floor)
    explain_lines = []
    if market_base_price is not None:
        explain_lines.append(f"市场基准 {market_base_price:.0f}")
    if age_adjusted_price is not None and age_discount_factor != 1.0:
        explain_lines.append(f"库龄系数 {age_discount_factor:.3f} -> {age_adjusted_price:.0f}")
    if cost_floor is not None:
        explain_lines.append(f"成本底线 {cost_floor:.0f}")
    if market_cap is not None:
        explain_lines.append(f"市场上限 {market_cap:.0f}")
    if manual_review["needs_manual_review"]:
        explain_lines.append(f"低于成本需人工确认，预计亏损 {manual_review['loss_amount']:.0f}")
    elif manual_review["below_target_profit"] and manual_review["target_profit_gap"] > 0:
        explain_lines.append(f"低于目标利润 {manual_review['target_profit_gap']:.0f}")
    if rule_engine is not None and item is not None and working_pricing is not None:
        explain_lines.extend([line for line in rule_engine.explain(item, working_pricing) if line])

    return {
        "pricing": working_pricing,
        "system_price": system_price,
        "system_settle_price": PricingEngine.calc_settle_price(system_price) if system_price is not None else None,
        "final_price": final_price,
        "final_settle_price": manual_review["settle_price"],
        "custom_mode": mode,
        "custom_value": value,
        "custom_enabled": custom_enabled,
        "custom_summary": _describe_custom_reprice_offset(mode, value),
        "rule_hit": getattr(working_pricing, "rule_hit", "") if working_pricing is not None else "",
        "floor_guard": market_floor,
        "market_floor": market_floor,
        "market_cap": market_cap,
        "cost_floor": cost_floor,
        "age_discount_factor": age_discount_factor,
        "age_adjusted_price": age_adjusted_price,
        "market_base_price": market_base_price,
        "below_cost": manual_review["below_cost"],
        "below_target_profit": manual_review["below_target_profit"],
        "needs_manual_review": manual_review["needs_manual_review"],
        "manual_review_reason": manual_review["manual_review_reason"],
        "loss_amount": manual_review["loss_amount"],
        "target_profit_gap": manual_review["target_profit_gap"],
        "price_gap_to_cost_floor": manual_review["price_gap_to_cost_floor"],
        "target_settle": manual_review["target_settle"],
        "cost_price": manual_review["cost_price"],
        "explain_lines": explain_lines,
    }


def _pricing_preview(pricing, rule_engine: Optional[RuleEngine] = None, item: Optional[BatchItem] = None, decision: Optional[dict] = None) -> str:
    if pricing is None:
        return "无定价结果"
    if decision is None:
        decision = build_reprice_decision(item, pricing, rule_engine, apply_rules=True)
    parts = [
        f"样本 {pricing.sample_count}",
        f"精确 {pricing.exact_sample_count}",
        f"补充 {pricing.fallback_sample_count}",
        f"极速价 {pricing.fast_price:.0f}" if pricing.fast_price is not None else "极速价 -",
        f"保守价 {pricing.conservative_price:.0f}" if pricing.conservative_price is not None else "保守价 -",
        f"市场底 {pricing.market_floor_price:.0f}" if pricing.market_floor_price is not None else (f"底价 {pricing.floor_price:.0f}" if pricing.floor_price is not None else "底价 -"),
        f"市场基准 {decision['market_base_price']:.0f}" if decision.get("market_base_price") is not None else "市场基准 -",
        f"库龄后 {decision['age_adjusted_price']:.0f}" if decision.get("age_adjusted_price") is not None else "库龄后 -",
        f"成本底线 {decision['cost_floor']:.0f}" if decision.get("cost_floor") is not None else "成本底线 -",
        f"系统建议价 {decision['system_price']:.0f}" if decision.get("system_price") is not None else "系统建议价 -",
        f"最终执行价 {decision['final_price']:.0f}" if decision.get("final_price") is not None else "最终执行价 -",
        f"自定义 {decision['custom_summary']}",
        f"预计到手 {decision['final_settle_price']:.0f}" if decision.get("final_settle_price") is not None else "预计到手 -",
        f"置信度 {getattr(pricing.confidence, 'value', pricing.confidence) or '-'}",
    ]
    if decision.get("needs_manual_review"):
        parts.append("需人工确认")
        if decision.get("loss_amount"):
            parts.append(f"预计亏损 {decision['loss_amount']:.0f}")
        if decision.get("manual_review_reason"):
            parts.append(f"原因 {decision['manual_review_reason']}")
    elif decision.get("below_target_profit") and decision.get("target_profit_gap"):
        parts.append(f"低于目标利润 {decision['target_profit_gap']:.0f}")
    if pricing.rule_hit:
        parts.append(f"规则 {pricing.rule_hit}")
    if pricing.warning:
        parts.append(f"提示 {pricing.warning}")
    explain_lines = decision.get("explain_lines") or []
    if explain_lines:
        parts.append("逻辑 " + " | ".join(explain_lines[:4]))
    return "；".join(parts)


def _normalize_lookup_code(value) -> str:
    return str(value or "").strip()


def _erp_item_lookup_codes(erp_item) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for raw in (getattr(erp_item, "qc_code", ""), getattr(erp_item, "imei", "")):
        code = _normalize_lookup_code(raw)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _detail_lookup_codes(detail) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    raw_values = [
        getattr(detail, "qc_code", ""),
        getattr(detail, "imei", ""),
    ]
    extra_codes = getattr(detail, "_lookup_codes", None) or []
    if isinstance(extra_codes, (list, tuple, set)):
        raw_values.extend(extra_codes)
    for raw in raw_values:
        code = _normalize_lookup_code(raw)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _query_detail_for_lookup_code(svc: ImeiService, code: str):
    normalized = _normalize_lookup_code(code)
    if not normalized:
        return None
    query_methods: list[Callable[[str], Optional[object]]] = []
    if normalized.isdigit() and len(normalized) == 15:
        query_methods.extend([svc.query_by_imei, svc.query_by_qc_code])
    else:
        query_methods.extend([svc.query_by_qc_code, svc.query_by_imei])
    for query in query_methods:
        try:
            detail = query(normalized)
        except Exception:
            detail = None
        if detail is None:
            continue
        lookup_codes = set(getattr(detail, "_lookup_codes", []) or [])
        lookup_codes.add(normalized)
        setattr(detail, "_lookup_codes", tuple(lookup_codes))
        return detail
    return None


def _match_detail_for_erp_item(svc: ImeiService, erp_item, detail_by_code: dict[str, object]):
    for code in _erp_item_lookup_codes(erp_item):
        detail = detail_by_code.get(code)
        if detail is not None:
            return detail

    for code in _erp_item_lookup_codes(erp_item):
        detail = _query_detail_for_lookup_code(svc, code)
        if detail is None:
            continue
        for detail_code in _detail_lookup_codes(detail):
            detail_by_code.setdefault(detail_code, detail)
        detail_by_code.setdefault(code, detail)
        return detail
    return None


def _build_imported_batch_item(detail, account_name: str, erp_item) -> BatchItem:
    qc_code = _normalize_lookup_code(getattr(detail, "qc_code", "")) or _normalize_lookup_code(getattr(erp_item, "qc_code", ""))
    imei = _normalize_lookup_code(getattr(detail, "imei", "")) or _normalize_lookup_code(getattr(erp_item, "imei", ""))
    return BatchItem(
        product_id=detail.product_id,
        qc_code=qc_code or imei,
        title=detail.title,
        current_price=detail.current_price,
        status=detail.status,
        status_detail=_status_detail_from_obj(detail),
        account_name=account_name,
        imei=imei,
        model=detail.model,
        condition=detail.condition,
        capacity=detail.capacity,
        color=detail.color,
        cost_price=0.0,
        listed_time=detail.listed_time,
        settle_price=detail.settle_price,
        op_status="已导入",
        op_message="ERP 同步导入",
    )


def _match_erp_items_for_account(account, erp_items, on_progress: Optional[Callable[[str], None]] = None) -> tuple[list[tuple[object, BatchItem]], list[object], bool]:
    if not erp_items:
        return [], [], False

    svc = ImeiService(account.name, account.cookie)
    lookup_codes: list[str] = []
    for erp_item in erp_items:
        lookup_codes.extend(_erp_item_lookup_codes(erp_item))

    if not lookup_codes:
        return [], list(erp_items), False

    try:
        details, _missing = svc.fetch_by_codes(lookup_codes)
    except Exception as e:
        _log(f"[{account.name}] 批量匹配失败: {e}", on_progress)
        return [], list(erp_items), True

    detail_by_code: dict[str, object] = {}
    for detail in details:
        for code in _detail_lookup_codes(detail):
            detail_by_code.setdefault(code, detail)

    matched: list[tuple[object, BatchItem]] = []
    unresolved: list[object] = []
    for erp_item in erp_items:
        detail = None
        for code in _erp_item_lookup_codes(erp_item):
            detail = detail_by_code.get(code)
            if detail is not None:
                break
        if detail is None:
            unresolved.append(erp_item)
            continue
        matched.append((erp_item, _build_imported_batch_item(detail, account.name, erp_item)))

    return matched, unresolved, False


# ─── 任务0：ERP 同步 ─────────────────────────────────────────

def task_erp_sync(
    erp_config: ErpConfig,
    cost_map: CostPriceMap,
    account_store: AccountStore,
    imported_store: Optional[BatchItemStore] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    同步 ERP 商品到本地成本价映射，并通过质检码/IMEI 匹配转转商品。
    """
    try:
        fetcher = ErpFetcher(erp_config)
        _log("开始从爱管机 ERP 拉取商品...", on_progress)
        fetcher.check_and_refresh_token()

        on_sale_erp = fetcher.fetch_on_sale_items()
        in_stock_erp = fetcher.fetch_in_stock_items()
        all_erp = on_sale_erp + in_stock_erp
        _log(f"ERP 商品拉取完成：上架 {len(on_sale_erp)} 件，在库 {len(in_stock_erp)} 件", on_progress)

        cost_data = fetcher.build_cost_map(all_erp)
        cost_map.update(cost_data)
        _log(f"已同步成本价 {len(cost_data)} 条", on_progress)

        accounts = account_store.enabled_accounts()
        if not accounts:
            existing_count = imported_store.count() if imported_store is not None else 0
            summary = f"已同步成本价 {len(cost_data)} 条，无可用转转账号，未执行导入"
            if imported_store is not None:
                summary += f"，商品管理保留现有 {existing_count} 件"
            _log(summary, on_progress)
            return {
                "items": [],
                "summary": summary,
                "synced": len(cost_data),
                "matched": 0,
                "missed": len(all_erp),
                "error": "",
            }

        matched_items: list[BatchItem] = []
        added_product_ids: set[str] = set()
        missed = 0
        error_accounts: list[str] = []
        total_items = len(all_erp)
        progress_step = 10 if total_items >= 100 else 5 if total_items >= 30 else 1

        _log(f"开始批量匹配 ERP 商品到转转，共 {total_items} 件，账号 {len(accounts)} 个...", on_progress)

        unmatched_items = list(all_erp)
        for account in accounts:
            if not unmatched_items:
                break
            matched_pairs, unresolved, had_error = _match_erp_items_for_account(account, unmatched_items, on_progress)
            if had_error:
                error_accounts.append(account.name)
            for erp_item, matched_item in matched_pairs:
                matched_item.cost_price = cost_map.get(matched_item.product_id) or getattr(erp_item, "cost_price", 0.0)
                if matched_item.product_id not in added_product_ids:
                    matched_items.append(matched_item)
                    added_product_ids.add(matched_item.product_id)
            unmatched_items = unresolved
            completed = total_items - len(unmatched_items)
            if total_items and (completed == total_items or completed % progress_step == 0):
                _log(
                    f"[{account.name}] 已匹配 {completed}/{total_items} 件 ERP 商品，成功 {len(matched_items)}，未找到 {len(unmatched_items)}...",
                    on_progress,
                )

        missed = len(unmatched_items)
        if imported_store is not None:
            # 与手动导入行为对齐：更新已存在商品，追加新商品，而不是整体覆盖
            existing_items = imported_store.get_all()
            existing_ids = {it.product_id for it in existing_items}
            new_items: list[BatchItem] = []
            for matched_item in matched_items:
                pid = matched_item.product_id
                if not pid:
                    continue
                if pid in existing_ids:
                    imported_store.update_item(
                        pid,
                        qc_code=matched_item.qc_code,
                        title=matched_item.title,
                        current_price=matched_item.current_price,
                        status=matched_item.status,
                        status_detail=_status_detail_from_obj(matched_item),
                        account_name=matched_item.account_name,
                        imei=getattr(matched_item, "imei", ""),
                        model=matched_item.model,
                        capacity=matched_item.capacity,
                        color=matched_item.color,
                        listed_time=matched_item.listed_time,
                        settle_price=matched_item.settle_price,
                        cost_price=matched_item.cost_price,
                    )
                else:
                    new_items.append(matched_item)
            if new_items:
                imported_store.extend(new_items)

        summary = f"同步成本价 {len(cost_data)} 条，匹配 {len(matched_items)} 件，未找到 {missed} 件"
        if imported_store is not None:
            summary += f"，已导入商品管理 {len(matched_items)} 件"
        if error_accounts:
            summary += f"（以下店铺匹配接口报错已跳过：{', '.join(error_accounts)}）"
        _log(summary, on_progress)
        return {
            "items": matched_items,
            "summary": summary,
            "synced": len(cost_data),
            "matched": len(matched_items),
            "missed": missed,
            "error": ", ".join(f"{name}: 匹配接口报错" for name in error_accounts) if error_accounts else "",
        }
    except Exception as e:
        logger.exception("ERP 同步失败")
        return {
            "items": [],
            "summary": str(e),
            "synced": 0,
            "matched": 0,
            "missed": 0,
            "error": str(e),
        }


# ─── 任务1：自动调价（仅导入商品） ────────────────────────────

def task_auto_reprice(
    account_store: AccountStore,
    sold_cache: SoldCache,
    cost_map: CostPriceMap,
    rule_engine: RuleEngine,
    imported_store: BatchItemStore,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    engine = PricingEngine()
    history_db = get_history_db()
    accounts = {account.name: account for account in account_store.enabled_accounts()}
    items = _iter_actionable_items(imported_store)
    total = ok = skip = fail = 0

    if not items:
        _log("导入商品列表中没有可处理商品，跳过自动调价", on_progress)
        return {"total": 0, "ok": 0, "skip": 0, "fail": 0}

    services: dict[str, ImeiService] = {}
    for item in items:
        total += 1
        account = accounts.get(item.account_name)
        if not account:
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg="账号不存在或未启用",
                op_status="失败",
                op_message="自动调价跳过",
            )
            _log(f"[{item.account_name or '-'}] [{item.qc_code or item.product_id}] 跳过: 账号不存在或未启用", on_progress)
            fail += 1
            continue

        svc = services.get(account.name)
        if svc is None:
            svc = ImeiService(account.name, account.cookie)
            services[account.name] = svc

        try:
            detail = _refresh_imported_detail(svc, item)
        except Exception as e:
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg=str(e),
                op_status="失败",
                op_message="刷新失败",
            )
            _log(f"[{account.name}] [{item.qc_code or item.product_id}] 刷新失败: {e}", on_progress)
            fail += 1
            continue

        if detail is None:
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg="未找到商品",
                op_status="失败",
                op_message="自动调价跳过",
            )
            _log(f"[{account.name}] [{item.qc_code or item.product_id}] 跳过: 未找到商品", on_progress)
            fail += 1
            continue

        current_cost = cost_map.get(detail.product_id) or item.cost_price
        imported_store.update_item(
            item.product_id,
            qc_code=detail.qc_code or item.qc_code,
            title=detail.title,
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            current_price=detail.current_price,
            status=detail.status,
            status_detail=_status_detail_from_obj(detail),
            listed_time=detail.listed_time,
            settle_price=detail.settle_price,
            cost_price=current_cost,
        )

        if detail.status != ProductStatus.ON_SALE:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=f"当前状态 {detail.status.label}",
                op_status="跳过",
                op_message="仅处理在售商品",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 当前状态 {detail.status.label}", on_progress)
            skip += 1
            continue

        records = sold_cache.filter_by_model_key(detail.model, detail.condition, detail.capacity, detail.color)
        pricing = engine.calculate(records, detail.model, detail.condition, detail.capacity, detail.color)
        work_item = BatchItem(
            product_id=detail.product_id,
            qc_code=detail.qc_code or item.qc_code,
            title=detail.title,
            current_price=detail.current_price,
            status=detail.status,
            account_name=account.name,
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            cost_price=current_cost,
            listed_time=detail.listed_time,
            pricing=pricing,
        )
        decision = build_reprice_decision(work_item, pricing, rule_engine, apply_rules=True)
        system_price = decision.get("system_price")
        new_price = decision["final_price"]
        preview = _pricing_preview(pricing, rule_engine, work_item, decision)
        imported_store.update_item(
            item.product_id,
            pricing=pricing,
            suggested_price=new_price,
            new_price=new_price,
            floor_price=pricing.floor_price,
            suggested_settle_price=decision.get("final_settle_price"),
            confidence=getattr(pricing.confidence, "value", pricing.confidence),
            rule_hit=decision.get("rule_hit", ""),
        )

        if decision.get("needs_manual_review"):
            manual_reason = decision.get("manual_review_reason") or "建议价低于成本，需人工确认"
            imported_store.update_item(
                item.product_id,
                suggested_price=new_price,
                new_price=new_price,
                suggested_settle_price=decision.get("final_settle_price"),
                reprice_ok=None,
                reprice_msg=f"{manual_reason}｜{preview}",
                op_status="待确认",
                op_message=f"自动调价已拦截，需人工确认｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 待人工确认: {manual_reason}｜{preview}", on_progress)
            skip += 1
            continue

        if new_price is None:
            imported_store.update_item(
                item.product_id,
                suggested_price=system_price,
                new_price=None,
                suggested_settle_price=decision.get("system_settle_price"),
                reprice_ok=None,
                reprice_msg=f"无定价依据｜{preview}",
                op_status="跳过",
                op_message=f"自动调价跳过｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 无定价依据｜{preview}", on_progress)
            skip += 1
            continue

        new_price = round(new_price, 0)
        suggested_settle = _estimate_suggested_settle_price(svc, detail.product_id, float(new_price))
        imported_store.update_item(
            item.product_id,
            suggested_price=new_price,
            new_price=new_price,
            suggested_settle_price=suggested_settle,
        )
        if abs(new_price - detail.current_price) < 1:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=f"差价小于 1 元｜{preview}",
                op_status="跳过",
                op_message=f"自动调价跳过｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 差价小于 1 元｜{preview}", on_progress)
            skip += 1
            continue

        success, msg = svc.change_price(detail, new_price)
        if success:
            ok += 1
            settle = PricingEngine.calc_settle_price(new_price)
            refreshed = _refresh_imported_detail(svc, item)
            history_db.record(PriceChangeRecord(
                id=None,
                timestamp=datetime.datetime.now(),
                product_id=detail.product_id,
                qc_code=detail.qc_code or item.qc_code,
                title=detail.title,
                model=detail.model,
                condition=detail.condition,
                capacity=detail.capacity,
                color=detail.color,
                old_price=detail.current_price,
                new_price=new_price,
                diff=new_price - detail.current_price,
                settle_price=settle or 0.0,
                trigger=PriceTrigger.AUTO_REPRICE,
                account_name=account.name,
                rule_hit=decision.get("rule_hit", ""),
            ))
            success_msg = msg or "自动调价成功"
            imported_store.update_item(
                item.product_id,
                qc_code=getattr(refreshed, "qc_code", detail.qc_code) or item.qc_code,
                title=getattr(refreshed, "title", detail.title),
                model=getattr(refreshed, "model", detail.model),
                condition=getattr(refreshed, "condition", detail.condition),
                capacity=getattr(refreshed, "capacity", detail.capacity),
                color=getattr(refreshed, "color", detail.color),
                current_price=getattr(refreshed, "current_price", new_price),
                settle_price=getattr(refreshed, "settle_price", settle) or settle,
                suggested_price=new_price,
                suggested_settle_price=getattr(refreshed, "settle_price", settle) or settle,
                listed_time=getattr(refreshed, "listed_time", detail.listed_time),
                status=getattr(refreshed, "status", detail.status),
                status_detail=_status_detail_from_obj(refreshed or detail),
                new_price=new_price,
                reprice_ok=True,
                reprice_msg=success_msg,
                op_status="已改价",
                op_message=f"{success_msg}｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] {detail.current_price:.0f} → {new_price:.0f} ✓｜{preview}", on_progress)
        else:
            fail += 1
            imported_store.update_item(
                item.product_id,
                suggested_price=new_price,
                new_price=new_price,
                suggested_settle_price=suggested_settle,
                reprice_ok=False,
                reprice_msg=f"{msg}｜{preview}",
                op_status="失败",
                op_message=f"自动调价失败｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 改价失败: {msg}｜{preview}", on_progress)

    return {"total": total, "ok": ok, "skip": skip, "fail": fail}


# ─── 任务2：滞销预警 + 自动降价（仅导入商品） ──────────────────

def task_stale_drop(
    account_store: AccountStore,
    sold_cache: SoldCache,
    rule_engine: RuleEngine,
    imported_store: BatchItemStore,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    history_db = get_history_db()
    engine = PricingEngine()
    accounts = {account.name: account for account in account_store.enabled_accounts()}
    items = _iter_actionable_items(imported_store)
    total = ok = skip = fail = 0

    if not items:
        _log("导入商品列表中没有可处理商品，跳过滞销降价", on_progress)
        return {"total": 0, "ok": 0, "skip": 0, "fail": 0}

    stage1 = cfg.stale_stage1_days
    stage2 = cfg.stale_stage2_days
    drop_pct = cfg.stale_stage2_drop_pct
    services: dict[str, ImeiService] = {}

    for item in items:
        total += 1
        account = accounts.get(item.account_name)
        if not account:
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg="账号不存在或未启用",
                op_status="失败",
                op_message="滞销任务跳过",
            )
            _log(f"[{item.account_name or '-'}] [{item.qc_code or item.product_id}] 跳过: 账号不存在或未启用", on_progress)
            fail += 1
            continue

        svc = services.get(account.name)
        if svc is None:
            svc = ImeiService(account.name, account.cookie)
            services[account.name] = svc

        try:
            detail = _refresh_imported_detail(svc, item)
        except Exception as e:
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg=str(e),
                op_status="失败",
                op_message="刷新失败",
            )
            _log(f"[{account.name}] [{item.qc_code or item.product_id}] 刷新失败: {e}", on_progress)
            fail += 1
            continue

        if detail is None:
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg="未找到商品",
                op_status="失败",
                op_message="滞销任务跳过",
            )
            _log(f"[{account.name}] [{item.qc_code or item.product_id}] 跳过: 未找到商品", on_progress)
            fail += 1
            continue

        imported_store.update_item(
            item.product_id,
            qc_code=detail.qc_code or item.qc_code,
            title=detail.title,
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            current_price=detail.current_price,
            status=detail.status,
            status_detail=_status_detail_from_obj(detail),
            listed_time=detail.listed_time,
            settle_price=detail.settle_price,
        )

        if detail.status != ProductStatus.ON_SALE:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=f"当前状态 {detail.status.label}",
                op_status="跳过",
                op_message="仅处理在售商品",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 当前状态 {detail.status.label}", on_progress)
            skip += 1
            continue
        if not detail.listed_time:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg="缺少上架时间",
                op_status="跳过",
                op_message="滞销任务跳过",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 缺少上架时间", on_progress)
            skip += 1
            continue

        stale_days = (datetime.datetime.now() - detail.listed_time).days
        if stale_days < stage1:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=f"在架 {stale_days} 天",
                op_status="跳过",
                op_message="未达滞销阈值",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 在架 {stale_days} 天，未达阈值 {stage1}", on_progress)
            skip += 1
            continue

        records = sold_cache.filter_by_model_key(detail.model, detail.condition, detail.capacity, detail.color)
        pricing = engine.calculate(records, detail.model, detail.condition, detail.capacity, detail.color)
        decision = build_reprice_decision(
            BatchItem(
                product_id=detail.product_id,
                qc_code=detail.qc_code or item.qc_code,
                title=detail.title,
                current_price=detail.current_price,
                status=detail.status,
                account_name=account.name,
                model=detail.model,
                condition=detail.condition,
                capacity=detail.capacity,
                color=detail.color,
                cost_price=item.cost_price,
                listed_time=detail.listed_time,
                settle_price=detail.settle_price,
                pricing=pricing,
            ),
            pricing,
            rule_engine,
            apply_rules=True,
        )
        preview = _pricing_preview(pricing, rule_engine, BatchItem(
            product_id=detail.product_id,
            qc_code=detail.qc_code or item.qc_code,
            title=detail.title,
            current_price=detail.current_price,
            status=detail.status,
            account_name=account.name,
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            cost_price=item.cost_price,
            listed_time=detail.listed_time,
            settle_price=detail.settle_price,
            pricing=pricing,
        ), decision)

        if stale_days >= stage2:
            base_price = decision.get("system_price") or pricing.cons_price or detail.current_price
            new_price = round(base_price * (1 - drop_pct / 100), 0)
            new_price = _clamp_price(new_price, decision.get("market_floor"), decision.get("market_cap"))
            stale_decision = _assess_manual_review(item, new_price, decision.get("cost_floor"))
            decision = {**decision, **stale_decision, "final_price": new_price, "final_settle_price": stale_decision.get("settle_price")}
            preview = _pricing_preview(pricing, rule_engine, BatchItem(
                product_id=detail.product_id,
                qc_code=detail.qc_code or item.qc_code,
                title=detail.title,
                current_price=detail.current_price,
                status=detail.status,
                account_name=account.name,
                model=detail.model,
                condition=detail.condition,
                capacity=detail.capacity,
                color=detail.color,
                cost_price=item.cost_price,
                listed_time=detail.listed_time,
                settle_price=detail.settle_price,
                pricing=pricing,
            ), decision)
            stage_label = "阶段2"
        else:
            base_price = decision.get("system_price") or pricing.cons_price
            if base_price is None or base_price >= detail.current_price:
                imported_store.update_item(
                    item.product_id,
                    pricing=pricing,
                    suggested_price=base_price,
                    new_price=None,
                    floor_price=pricing.floor_price,
                    suggested_settle_price=None,
                    confidence=getattr(pricing.confidence, "value", pricing.confidence),
                    rule_hit=pricing.rule_hit,
                    reprice_ok=None,
                    reprice_msg=f"保守价无下降空间｜{preview}",
                    op_status="跳过",
                    op_message=f"滞销任务跳过｜{preview}",
                )
                _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 保守价无下降空间｜{preview}", on_progress)
                skip += 1
                continue
            new_price = round(base_price, 0)
            new_price = _clamp_price(new_price, decision.get("market_floor"), decision.get("market_cap"))
            stage_decision = _assess_manual_review(item, new_price, decision.get("cost_floor"))
            decision = {**decision, **stage_decision, "final_price": new_price, "final_settle_price": stage_decision.get("settle_price")}
            preview = _pricing_preview(pricing, rule_engine, BatchItem(
                product_id=detail.product_id,
                qc_code=detail.qc_code or item.qc_code,
                title=detail.title,
                current_price=detail.current_price,
                status=detail.status,
                account_name=account.name,
                model=detail.model,
                condition=detail.condition,
                capacity=detail.capacity,
                color=detail.color,
                cost_price=item.cost_price,
                listed_time=detail.listed_time,
                settle_price=detail.settle_price,
                pricing=pricing,
            ), decision)
            stage_label = "阶段1"

        if decision.get("needs_manual_review"):
            manual_reason = decision.get("manual_review_reason") or f"{stage_label}建议价低于成本，需人工确认"
            imported_store.update_item(
                item.product_id,
                pricing=pricing,
                suggested_price=new_price,
                new_price=new_price,
                floor_price=pricing.floor_price,
                suggested_settle_price=decision.get("final_settle_price"),
                confidence=getattr(pricing.confidence, "value", pricing.confidence),
                rule_hit=pricing.rule_hit,
                reprice_ok=None,
                reprice_msg=f"{manual_reason}｜{preview}",
                op_status="待确认",
                op_message=f"{stage_label}自动降价已拦截，需人工确认｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 待人工确认: {manual_reason}｜{preview}", on_progress)
            skip += 1
            continue

        if abs(new_price - detail.current_price) < 1:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=f"差价小于 1 元｜{preview}",
                op_status="跳过",
                op_message=f"滞销任务跳过｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 差价小于 1 元｜{preview}", on_progress)
            skip += 1
            continue

        success, msg = svc.change_price(detail, new_price)
        if success:
            ok += 1
            settle = PricingEngine.calc_settle_price(new_price)
            refreshed = _refresh_imported_detail(svc, item)
            history_db.record(PriceChangeRecord(
                id=None,
                timestamp=datetime.datetime.now(),
                product_id=detail.product_id,
                qc_code=detail.qc_code or item.qc_code,
                title=detail.title,
                model=detail.model,
                condition=detail.condition,
                capacity=detail.capacity,
                color=detail.color,
                old_price=detail.current_price,
                new_price=new_price,
                diff=new_price - detail.current_price,
                settle_price=settle or 0.0,
                trigger=PriceTrigger.AUTO_STALE,
                account_name=account.name,
                note=f"滞销 {stale_days} 天，{stage_label}降价",
            ))
            success_msg = msg or f"{stage_label}降价成功"
            imported_store.update_item(
                item.product_id,
                qc_code=getattr(refreshed, "qc_code", detail.qc_code) or item.qc_code,
                title=getattr(refreshed, "title", detail.title),
                model=getattr(refreshed, "model", detail.model),
                condition=getattr(refreshed, "condition", detail.condition),
                capacity=getattr(refreshed, "capacity", detail.capacity),
                color=getattr(refreshed, "color", detail.color),
                current_price=getattr(refreshed, "current_price", new_price),
                settle_price=getattr(refreshed, "settle_price", settle) or settle,
                suggested_price=new_price,
                suggested_settle_price=getattr(refreshed, "settle_price", settle) or settle,
                listed_time=getattr(refreshed, "listed_time", detail.listed_time),
                status=getattr(refreshed, "status", detail.status),
                status_detail=_status_detail_from_obj(refreshed or detail),
                new_price=new_price,
                reprice_ok=True,
                reprice_msg=success_msg,
                op_status="已改价",
                op_message=f"{stage_label}自动降价成功｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 滞销 {stale_days} 天 {detail.current_price:.0f} → {new_price:.0f} ✓｜{preview}", on_progress)
        else:
            fail += 1
            imported_store.update_item(
                item.product_id,
                suggested_price=new_price,
                new_price=new_price,
                suggested_settle_price=decision.get("final_settle_price"),
                reprice_ok=False,
                reprice_msg=f"{msg}｜{preview}",
                op_status="失败",
                op_message=f"{stage_label}自动降价失败｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 滞销降价失败: {msg}｜{preview}", on_progress)

    return {"total": total, "ok": ok, "skip": skip, "fail": fail}


# ─── 任务3：未上架商品自动定价上架 ──────────────────────────

def task_auto_list(
    account_store: AccountStore,
    erp_config: ErpConfig,
    sold_cache: SoldCache,
    rule_engine: RuleEngine,
    imported_store: BatchItemStore,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """仅对导入商品管理中的未上架商品执行自动定价上架"""
    engine = PricingEngine()
    history_db = get_history_db()
    accounts = {account.name: account for account in account_store.enabled_accounts()}
    items = _iter_actionable_items(imported_store)
    ok = fail = skip = 0

    def log(msg):
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    if not items:
        log("导入商品列表中没有可处理商品，跳过未上架自动上架")
        return {"ok": 0, "fail": 0, "skip": 0, "error": ""}

    services: dict[str, ImeiService] = {}
    for item in items:
        account = accounts.get(item.account_name)
        if not account:
            imported_store.update_item(
                item.product_id,
                op_status="失败",
                op_message="账号不存在或未启用",
                reprice_ok=False,
                reprice_msg="账号不存在或未启用",
            )
            fail += 1
            continue

        svc = services.get(account.name)
        if svc is None:
            svc = ImeiService(account.name, account.cookie)
            services[account.name] = svc

        try:
            detail = _refresh_imported_detail(svc, item)
        except Exception as e:
            imported_store.update_item(
                item.product_id,
                op_status="失败",
                op_message="刷新失败",
                reprice_ok=False,
                reprice_msg=str(e),
            )
            fail += 1
            continue

        if detail is None:
            imported_store.update_item(
                item.product_id,
                op_status="失败",
                op_message="未找到商品",
                reprice_ok=False,
                reprice_msg="未找到商品",
            )
            fail += 1
            continue

        imported_store.update_item(
            item.product_id,
            qc_code=detail.qc_code or item.qc_code,
            title=detail.title,
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            current_price=detail.current_price,
            status=detail.status,
            status_detail=_status_detail_from_obj(detail),
            listed_time=detail.listed_time,
            settle_price=detail.settle_price,
        )

        if detail.status != ProductStatus.NOT_LISTED:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=f"当前状态 {detail.status.label}",
                op_status="跳过",
                op_message="仅处理未上架商品",
            )
            skip += 1
            continue

        records = sold_cache.filter_by_model_key(detail.model, detail.condition, detail.capacity, detail.color)
        pricing = engine.calculate(records, detail.model, detail.condition, detail.capacity, detail.color)
        work_item = BatchItem(
            product_id=detail.product_id,
            qc_code=detail.qc_code or item.qc_code,
            title=detail.title,
            current_price=detail.current_price,
            status=detail.status,
            account_name=account.name,
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            cost_price=item.cost_price,
            listed_time=detail.listed_time,
            settle_price=detail.settle_price,
            pricing=pricing,
        )
        decision = build_reprice_decision(work_item, pricing, rule_engine, apply_rules=True)
        price = decision.get("final_price")
        preview = _pricing_preview(pricing, rule_engine, work_item, decision)
        if price is None:
            imported_store.update_item(
                item.product_id,
                pricing=pricing,
                suggested_price=None,
                new_price=None,
                floor_price=pricing.floor_price,
                suggested_settle_price=None,
                confidence=getattr(pricing.confidence, "value", pricing.confidence),
                rule_hit=pricing.rule_hit,
                op_status="跳过",
                op_message=f"无定价依据，未执行自动上架｜{preview}",
                reprice_ok=None,
                reprice_msg=f"无定价依据｜{preview}",
            )
            skip += 1
            continue

        price = round(price, 0)
        if decision.get("needs_manual_review"):
            imported_store.update_item(
                item.product_id,
                pricing=pricing,
                suggested_price=price,
                new_price=price,
                floor_price=pricing.floor_price,
                suggested_settle_price=decision.get("final_settle_price"),
                confidence=getattr(pricing.confidence, "value", pricing.confidence),
                rule_hit=pricing.rule_hit,
                op_status="待确认",
                op_message=f"自动上架已拦截，需人工确认｜{preview}",
                reprice_ok=None,
                reprice_msg=f"{decision.get('manual_review_reason') or '建议价低于成本，需人工确认'}｜{preview}",
            )
            log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 待人工确认: {preview}")
            skip += 1
            continue

        suggested_settle = _estimate_suggested_settle_price(svc, detail.product_id, float(price))
        imported_store.update_item(
            item.product_id,
            pricing=pricing,
            suggested_price=price,
            new_price=price,
            floor_price=pricing.floor_price,
            suggested_settle_price=suggested_settle,
            confidence=getattr(pricing.confidence, "value", pricing.confidence),
            rule_hit=pricing.rule_hit,
        )
        success, msg = svc.list_product(detail.product_id, price, detail.qc_code)
        if success:
            ok += 1
            settle = PricingEngine.calc_settle_price(price)
            refreshed = _refresh_imported_detail(svc, item) or detail
            imported_store.update_item(
                item.product_id,
                qc_code=getattr(refreshed, "qc_code", detail.qc_code) or item.qc_code,
                title=getattr(refreshed, "title", detail.title),
                model=getattr(refreshed, "model", detail.model),
                condition=getattr(refreshed, "condition", detail.condition),
                capacity=getattr(refreshed, "capacity", detail.capacity),
                color=getattr(refreshed, "color", detail.color),
                current_price=getattr(refreshed, "current_price", price),
                settle_price=getattr(refreshed, "settle_price", settle) or settle,
                suggested_price=price,
                suggested_settle_price=getattr(refreshed, "settle_price", settle) or settle,
                listed_time=getattr(refreshed, "listed_time", detail.listed_time),
                status=getattr(refreshed, "status", detail.status),
                status_detail=_status_detail_from_obj(refreshed or detail),
                pricing=pricing,
                new_price=price,
                reprice_ok=True,
                reprice_msg=msg or "自动上架成功",
                op_status="已上架",
                op_message=f"自动上架成功，定价 ¥{price:,.0f}｜{preview}",
            )
            log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 定价 {price:.0f} 自动上架成功")
            history_db.record(PriceChangeRecord(
                id=None,
                timestamp=datetime.datetime.now(),
                product_id=detail.product_id,
                qc_code=detail.qc_code or item.qc_code,
                title=detail.title,
                model=detail.model,
                condition=detail.condition,
                capacity=detail.capacity,
                color=detail.color,
                old_price=0,
                new_price=price,
                diff=price,
                settle_price=PricingEngine.calc_settle_price(price),
                trigger=PriceTrigger.AUTO_LIST,
                account_name=account.name,
            ))
        else:
            fail += 1
            imported_store.update_item(
                item.product_id,
                suggested_price=price,
                new_price=price,
                suggested_settle_price=suggested_settle,
                reprice_ok=False,
                reprice_msg=msg or "自动上架失败",
                op_status="失败",
                op_message=(msg or "自动上架失败") + f"｜{preview}",
            )
            log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 自动上架失败: {msg}")

    return {"ok": ok, "fail": fail, "skip": skip}



# ─── 任务4：销售播报 ─────────────────────────────────────────

def task_sales_report(
    account_store: AccountStore,
    notifier,
    on_progress: Optional[Callable[[str], None]] = None,
) -> str:
    """
    生成今日销售播报文本并发送
    :return: 播报文本
    """
    sold_cache = SoldCache()
    records = sold_cache.load()

    today = datetime.date.today()
    today_records = [
        r for r in records
        if r.sold_time.date() == today
    ]

    count = len(today_records)
    total_amount = sum(r.sold_price for r in today_records)
    avg_price = total_amount / count if count else 0

    report = (
        f"📊 今日销售播报 {today.strftime('%m/%d')}\n"
        f"成交：{count} 台\n"
        f"金额：¥{total_amount:,.0f}\n"
        f"均价：¥{avg_price:,.0f}"
    )

    if hasattr(notifier, "send_text"):
        notifier.send_text(report)

    if on_progress:
        on_progress(report)
    return report
