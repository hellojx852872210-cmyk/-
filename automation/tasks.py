# -*- coding: utf-8 -*-
"""
自动化任务实现
每个任务函数由 scheduler 定期调用，或通过企业微信指令触发
"""
from __future__ import annotations

import datetime
import logging
import uuid
from typing import Callable, Optional

from ..config import cfg
from ..core.batch_store import BatchItemStore
from ..core.models import BatchItem, PriceChangeRecord, PriceTrigger, ProductStatus
from ..core.price_history import get_history_db
from ..core.pricing_engine import PricingEngine
from ..core.rule_engine import RuleEngine
from ..core.utils import round_to_8
from ..services.data_store import AccountStore, CostPriceMap, SoldCache
from ..services.erp_service import ErpConfig, ErpFetcher
from ..services.reprice_service import (
    assess_manual_review as _assess_manual_review,
    build_reprice_decision,
    clamp_price as _clamp_price,
    pricing_preview as _pricing_preview,
    recalc_decision_with_final_price,
    run_reprice_pipeline,
)
from ..services.zhuanzhuan_api import DataFetcher, ImeiService

logger = logging.getLogger(__name__)


def _log(msg: str, on_progress: Optional[Callable[[str], None]] = None) -> None:
    logger.info(msg)
    if on_progress:
        on_progress(msg)


def _account_login_state(
    svc: ImeiService,
    account_name: str,
    login_state_cache: dict[str, tuple[bool, str]],
) -> tuple[bool, str]:
    if account_name in login_state_cache:
        return login_state_cache[account_name]
    try:
        ok, reason = svc.check_cookie_valid()
    except Exception as exc:
        ok, reason = False, str(exc)
    login_state_cache[account_name] = (ok, reason)
    return ok, reason


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


def _resolve_final_settle_price(
    svc: ImeiService,
    product_id: str,
    final_price: Optional[float],
    refreshed_detail=None,
) -> Optional[float]:
    refreshed_settle = getattr(refreshed_detail, "settle_price", None) if refreshed_detail is not None else None
    if refreshed_settle is not None:
        return float(refreshed_settle)
    if product_id and final_price is not None:
        estimated = _estimate_suggested_settle_price(svc, product_id, float(final_price))
        if estimated is not None:
            return estimated
    if final_price is None:
        return None
    return PricingEngine.calc_settle_price(final_price)


def _normalize_tail8_price(price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    rounded = int(round(float(price), 0))
    if rounded <= 0:
        return 0.0
    return float(round_to_8(rounded))


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


def _sample_codes(codes: list[str], limit: int = 5) -> str:
    values = [code for code in codes if code]
    if not values:
        return "-"
    sample = values[:limit]
    suffix = " ..." if len(values) > limit else ""
    return ", ".join(sample) + suffix


def _format_erp_item_identity(erp_item) -> str:
    return (
        f"product_id={getattr(erp_item, 'product_id', '') or '-'} "
        f"qc={getattr(erp_item, 'qc_code', '') or '-'} "
        f"imei={getattr(erp_item, 'imei', '') or '-'}"
    )


def _erp_code_stats(erp_items) -> dict[str, object]:
    total_items = len(erp_items or [])
    qc_count = 0
    imei_count = 0
    no_code_count = 0
    raw_lookup_count = 0
    unique_codes: list[str] = []
    seen: set[str] = set()
    sample_items: list[str] = []

    for erp_item in erp_items or []:
        qc_code = _normalize_lookup_code(getattr(erp_item, "qc_code", ""))
        imei = _normalize_lookup_code(getattr(erp_item, "imei", ""))
        if qc_code:
            qc_count += 1
        if imei:
            imei_count += 1
        item_codes = _erp_item_lookup_codes(erp_item)
        if not item_codes:
            no_code_count += 1
            if len(sample_items) < 5:
                sample_items.append(_format_erp_item_identity(erp_item))
            continue
        raw_lookup_count += len(item_codes)
        for code in item_codes:
            if code in seen:
                continue
            seen.add(code)
            unique_codes.append(code)

    return {
        "total_items": total_items,
        "qc_count": qc_count,
        "imei_count": imei_count,
        "no_code_count": no_code_count,
        "raw_lookup_count": raw_lookup_count,
        "unique_lookup_count": len(unique_codes),
        "sample_codes": _sample_codes(unique_codes),
        "sample_no_code_items": sample_items,
    }


def _query_detail_for_lookup_code(
    svc: ImeiService,
    code: str,
    lookup_cache: Optional[dict[str, tuple[Optional[object], str]]] = None,
) -> tuple[Optional[object], str]:
    normalized = _normalize_lookup_code(code)
    if not normalized:
        return None, "lookup code 为空"
    if lookup_cache is not None and normalized in lookup_cache:
        return lookup_cache[normalized]
    query_methods: list[Callable[[str], Optional[object]]] = []
    if normalized.isdigit() and len(normalized) == 15:
        query_methods.extend([svc.query_by_imei, svc.query_by_qc_code])
    else:
        query_methods.extend([svc.query_by_qc_code, svc.query_by_imei])
    errors: list[str] = []
    for query in query_methods:
        try:
            detail = query(normalized)
        except Exception as exc:
            errors.append(f"{query.__name__}: {exc}")
            detail = None
        if detail is None:
            continue
        lookup_codes = set(getattr(detail, "_lookup_codes", []) or [])
        lookup_codes.add(normalized)
        setattr(detail, "_lookup_codes", tuple(lookup_codes))
        result = (detail, "")
        if lookup_cache is not None:
            lookup_cache[normalized] = result
        return result
    result = (None, "；".join(errors))
    if lookup_cache is not None:
        lookup_cache[normalized] = result
    return result


def _match_detail_for_erp_item(
    svc: ImeiService,
    erp_item,
    detail_by_code: dict[str, object],
    missing_codes: Optional[set[str]] = None,
    lookup_cache: Optional[dict[str, tuple[Optional[object], str]]] = None,
) -> tuple[Optional[object], str]:
    codes = _erp_item_lookup_codes(erp_item)
    if not codes:
        return None, "ERP 商品缺少质检码/IMEI"

    for code in codes:
        detail = detail_by_code.get(code)
        if detail is not None:
            return detail, f"批量结果命中 {code}"

    missing_hits = [code for code in codes if code in (missing_codes or set())]
    if missing_hits and len(missing_hits) == len(codes):
        return None, f"批量接口未返回 {', '.join(missing_hits)}；未找到 lookup code: {', '.join(codes)}"

    fallback_errors: list[str] = []
    for code in codes:
        detail, error = _query_detail_for_lookup_code(svc, code, lookup_cache=lookup_cache)
        if detail is None:
            if error:
                fallback_errors.append(f"{code} => {error}")
            continue
        for detail_code in _detail_lookup_codes(detail):
            detail_by_code.setdefault(detail_code, detail)
        detail_by_code.setdefault(code, detail)
        return detail, f"单条兜底命中 {code}"

    reason_parts: list[str] = []
    if missing_hits:
        reason_parts.append(f"批量接口未返回 {', '.join(missing_hits)}")
    if fallback_errors:
        reason_parts.append(f"单条查询异常 { ' | '.join(fallback_errors) }")
    reason_parts.append(f"未找到 lookup code: {', '.join(codes)}")
    return None, "；".join(reason_parts)




def _is_listing_eligible(item: Optional[BatchItem]) -> bool:
    return bool(getattr(item, "listing_eligible", False))


def _allowed_listing_import_sources() -> set[str]:
    return {"manual", "erp"}


def _listing_skip_reason(item: Optional[BatchItem]) -> str:
    source = str(getattr(item, "import_source", "") or "").strip().lower()
    if getattr(item, "ignored", False):
        return "商品在不处理区"
    if not getattr(item, "selected", True):
        return "商品未勾选参与自动化"
    if source not in _allowed_listing_import_sources():
        return f"导入来源不支持自动上架：{source or '-'}"
    if getattr(item, "status", None) != ProductStatus.NOT_LISTED:
        status = getattr(getattr(item, "status", None), "label", "未知状态")
        return f"当前状态非未上架：{status}"
    if not _is_listing_eligible(item):
        return "商品未标记为可自动上架"
    return ""


def _manual_review_fields(decision: Optional[dict]) -> dict:
    data = decision or {}
    return {
        "needs_manual_review": bool(data.get("needs_manual_review")),
        "manual_review_reason": str(data.get("manual_review_reason") or ""),
    }


def _build_imported_batch_item(detail, account_name: str, erp_item) -> BatchItem:
    status_detail = _status_detail_from_obj(detail)
    item = BatchItem(
        product_id=str(getattr(detail, "product_id", "") or getattr(erp_item, "product_id", "") or ""),
        qc_code=str(getattr(detail, "qc_code", "") or getattr(erp_item, "qc_code", "") or ""),
        title=str(getattr(detail, "title", "") or getattr(erp_item, "title", "") or ""),
        current_price=float(getattr(detail, "current_price", 0.0) or 0.0),
        status=getattr(detail, "status", ProductStatus.UNKNOWN),
        status_detail=status_detail,
        account_name=account_name,
        imei=str(getattr(detail, "imei", "") or getattr(erp_item, "imei", "") or ""),
        model=str(getattr(detail, "model", "") or ""),
        condition=str(getattr(detail, "condition", "") or ""),
        capacity=str(getattr(detail, "capacity", "") or ""),
        color=str(getattr(detail, "color", "") or ""),
        cost_price=float(getattr(erp_item, "cost_price", 0.0) or 0.0),
        listed_time=getattr(detail, "listed_time", None) or getattr(erp_item, "listed_time", None),
        settle_price=float(getattr(detail, "settle_price", 0.0) or 0.0),
        import_source="erp",
        listing_eligible=getattr(detail, "status", None) == ProductStatus.NOT_LISTED,
    )
    item.op_status = "待处理"
    item.op_message = status_detail
    return item


def _match_erp_items_for_account(
    account,
    erp_items,
    on_progress: Optional[Callable[[str], None]] = None,
    *,
    run_id: str,
) -> tuple[list[tuple[object, BatchItem]], list[object], bool]:
    if not erp_items:
        return [], [], False

    svc = ImeiService(account.name, account.cookie)
    stats = _erp_code_stats(erp_items)
    _log(
        f"[ERP_SYNC:{run_id}] [{account.name}] 开始账号匹配：ERP商品 {stats['total_items']} 件，"
        f"qc {stats['qc_count']} 件，imei {stats['imei_count']} 件，无code {stats['no_code_count']} 件，"
        f"lookup原始 {stats['raw_lookup_count']} 个，去重后 {stats['unique_lookup_count']} 个，样例 {stats['sample_codes']}",
        on_progress,
    )
    if stats["sample_no_code_items"]:
        for item_desc in stats["sample_no_code_items"]:
            _log(f"[ERP_SYNC:{run_id}] [{account.name}] 无lookup code样例：{item_desc}", on_progress)

    lookup_codes: list[str] = []
    for erp_item in erp_items:
        lookup_codes.extend(_erp_item_lookup_codes(erp_item))

    if not lookup_codes:
        _log(f"[ERP_SYNC:{run_id}] [{account.name}] 跳过批量匹配：本账号 ERP 商品均未提取到 lookup code", on_progress)
        return [], list(erp_items), False

    try:
        details, missing = svc.fetch_by_codes(lookup_codes, batch_size=20)
        _log(
            f"[ERP_SYNC:{run_id}] [{account.name}] 批量匹配返回：明细 {len(details)} 件，缺失code {len(missing or [])} 个",
            on_progress,
        )
    except Exception as e:
        _log(f"[ERP_SYNC:{run_id}] [{account.name}] 批量匹配失败: {e}", on_progress)
        return [], list(erp_items), True

    missing_codes = set(missing or [])
    detail_by_code: dict[str, object] = {}
    lookup_cache: dict[str, tuple[Optional[object], str]] = {}
    for detail in details:
        for code in _detail_lookup_codes(detail):
            detail_by_code.setdefault(code, detail)

    matched: list[tuple[object, BatchItem]] = []
    unresolved: list[object] = []
    unresolved_logs: list[str] = []
    progress_step = 50 if len(erp_items) >= 200 else 20 if len(erp_items) >= 80 else 10 if len(erp_items) >= 20 else 1
    for index, erp_item in enumerate(erp_items, start=1):
        if index == 1 or index == len(erp_items) or index % progress_step == 0:
            _log(
                f"[ERP_SYNC:{run_id}] [{account.name}] 正在整理匹配结果 {index}/{len(erp_items)}，"
                f"当前已匹配 {len(matched)}，待确认 {len(unresolved)}",
                on_progress,
            )
        detail, reason = _match_detail_for_erp_item(svc, erp_item, detail_by_code, missing_codes=missing_codes, lookup_cache=lookup_cache)
        if detail is None:
            unresolved.append(erp_item)
            codes = ", ".join(_erp_item_lookup_codes(erp_item)) or "-"
            unresolved_logs.append(
                f"[ERP_SYNC:{run_id}] [{account.name}] 未匹配 ERP 商品"
                f" product_id={getattr(erp_item, 'product_id', '') or '-'}"
                f" qc={getattr(erp_item, 'qc_code', '') or '-'}"
                f" imei={getattr(erp_item, 'imei', '') or '-'}"
                f" lookup={codes}"
                f" 原因={reason or '未知'}"
            )
            continue
        matched.append((erp_item, _build_imported_batch_item(detail, account.name, erp_item)))

    _log(
        f"[ERP_SYNC:{run_id}] [{account.name}] 账号匹配结束：匹配 {len(matched)} 件，未匹配 {len(unresolved)} 件，"
        f"detail_by_code {len(detail_by_code)} 个，missing_code {len(missing_codes)} 个",
        on_progress,
    )
    if unresolved_logs:
        preview_count = min(20, len(unresolved_logs))
        for line in unresolved_logs[:preview_count]:
            _log(line, on_progress)
        remaining = len(unresolved_logs) - preview_count
        if remaining > 0:
            _log(f"[ERP_SYNC:{run_id}] [{account.name}] 其余未匹配明细省略 {remaining} 条，请按上面样例继续排查", on_progress)

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
    run_id = uuid.uuid4().hex[:8]
    try:
        fetcher = ErpFetcher(erp_config)
        existing_count_before = imported_store.count() if imported_store is not None else 0
        accounts = account_store.enabled_accounts()
        _log(
            f"[ERP_SYNC:{run_id}] 开始同步：账号 {len(accounts)} 个，商品管理现有 {existing_count_before} 件",
            on_progress,
        )
        _log(f"[ERP_SYNC:{run_id}] 开始从爱管机 ERP 拉取商品...", on_progress)
        fetcher.check_and_refresh_token()

        on_sale_erp = fetcher.fetch_on_sale_items()
        in_stock_erp = fetcher.fetch_in_stock_items()
        all_erp = on_sale_erp + in_stock_erp
        _log(
            f"[ERP_SYNC:{run_id}] ERP 商品拉取完成：上架 {len(on_sale_erp)} 件，在库 {len(in_stock_erp)} 件，合计 {len(all_erp)} 件",
            on_progress,
        )

        erp_stats = _erp_code_stats(all_erp)
        _log(
            f"[ERP_SYNC:{run_id}] ERP 字段统计：qc {erp_stats['qc_count']} 件，imei {erp_stats['imei_count']} 件，"
            f"无code {erp_stats['no_code_count']} 件，lookup原始 {erp_stats['raw_lookup_count']} 个，"
            f"去重后 {erp_stats['unique_lookup_count']} 个，样例 {erp_stats['sample_codes']}",
            on_progress,
        )
        if erp_stats["sample_no_code_items"]:
            for item_desc in erp_stats["sample_no_code_items"]:
                _log(f"[ERP_SYNC:{run_id}] 无lookup code样例：{item_desc}", on_progress)

        cost_data = fetcher.build_cost_map(all_erp)
        cost_map.update(cost_data)
        _log(f"[ERP_SYNC:{run_id}] 已同步成本价 {len(cost_data)} 条", on_progress)

        if not accounts:
            summary = f"已同步成本价 {len(cost_data)} 条，无可用转转账号，未执行导入"
            if imported_store is not None:
                summary += f"，商品管理保留现有 {existing_count_before} 件"
            _log(f"[ERP_SYNC:{run_id}] {summary}", on_progress)
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

        _log(f"[ERP_SYNC:{run_id}] 开始批量匹配 ERP 商品到转转，共 {total_items} 件，账号 {len(accounts)} 个...", on_progress)

        unmatched_items = list(all_erp)
        for account in accounts:
            if not unmatched_items:
                break
            _log(f"[ERP_SYNC:{run_id}] [{account.name}] 准备匹配：当前待匹配 ERP 商品 {len(unmatched_items)} 件", on_progress)
            matched_pairs, unresolved, had_error = _match_erp_items_for_account(account, unmatched_items, on_progress, run_id=run_id)
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
                    f"[ERP_SYNC:{run_id}] [{account.name}] 已匹配 {completed}/{total_items} 件 ERP 商品，成功 {len(matched_items)}，未找到 {len(unmatched_items)}...",
                    on_progress,
                )

        missed = len(unmatched_items)
        updated_count = 0
        inserted_count = 0
        existing_count_after = existing_count_before
        if imported_store is not None:
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
                    updated_count += 1
                else:
                    new_items.append(matched_item)
            if new_items:
                imported_store.extend(new_items)
                inserted_count = len(new_items)
            existing_count_after = imported_store.count()
            _log(
                f"[ERP_SYNC:{run_id}] 商品管理写入完成：更新 {updated_count} 件，新增 {inserted_count} 件，"
                f"导入前 {existing_count_before} 件，导入后 {existing_count_after} 件",
                on_progress,
            )

        summary = f"同步成本价 {len(cost_data)} 条，匹配 {len(matched_items)} 件，未找到 {missed} 件"
        if imported_store is not None:
            summary += f"，已导入商品管理 {existing_count_after} 件"
        if error_accounts:
            summary += f"（以下店铺匹配接口报错已跳过：{', '.join(error_accounts)}）"
        _log(f"[ERP_SYNC:{run_id}] {summary}", on_progress)
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
        _log(f"[ERP_SYNC:{run_id}] 同步失败: {e}", on_progress)
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
    history_db = get_history_db()
    accounts = {account.name: account for account in account_store.enabled_accounts()}
    items = _iter_actionable_items(imported_store)
    total = ok = skip = fail = 0

    if not items:
        _log("导入商品列表中没有可处理商品，跳过自动调价", on_progress)
        return {"total": 0, "ok": 0, "skip": 0, "fail": 0}

    services: dict[str, ImeiService] = {}
    login_state_cache: dict[str, tuple[bool, str]] = {}
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

        login_ok, login_reason = _account_login_state(svc, account.name, login_state_cache)
        if not login_ok:
            reason_text = str(login_reason or "登录状态过期，请重新登录").strip()
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg=f"账号登录失效：{reason_text}",
                op_status="失败",
                op_message=f"自动调价跳过：账号登录失效（{reason_text}）",
            )
            _log(f"[{account.name}] [{item.qc_code or item.product_id}] 跳过: 账号登录失效（{reason_text}）", on_progress)
            fail += 1
            continue

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

        work_item = BatchItem(
            product_id=detail.product_id,
            qc_code=detail.qc_code or item.qc_code,
            title=detail.title,
            current_price=detail.current_price,
            status=detail.status,
            account_name=account.name,
            imei=getattr(detail, "imei", "") or getattr(item, "imei", ""),
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            cost_price=cost_map.get(detail.product_id) or item.cost_price,
            listed_time=detail.listed_time,
            settle_price=detail.settle_price,
        )
        imported_store.update_item(
            item.product_id,
            qc_code=work_item.qc_code,
            title=work_item.title,
            model=work_item.model,
            condition=work_item.condition,
            capacity=work_item.capacity,
            color=work_item.color,
            current_price=work_item.current_price,
            status=work_item.status,
            status_detail=_status_detail_from_obj(detail),
            listed_time=work_item.listed_time,
            settle_price=work_item.settle_price,
            cost_price=work_item.cost_price,
            imei=work_item.imei,
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

        pipeline = run_reprice_pipeline(
            work_item,
            sold_cache,
            rule_engine,
            apply_rules=True,
            settle_price_estimator=lambda price, product_id=detail.product_id: _estimate_suggested_settle_price(svc, product_id, price),
        )
        pricing = pipeline["pricing"]
        decision = pipeline["decision"]
        preview = pipeline["preview"]
        system_price = decision.get("system_price")
        new_price = pipeline["suggested_price"]
        suggested_settle = pipeline.get("suggested_settle_price")
        imported_store.update_item(
            item.product_id,
            pricing=pricing,
            suggested_price=new_price,
            new_price=new_price,
            floor_price=pricing.floor_price if pricing is not None else None,
            suggested_settle_price=suggested_settle,
            confidence=getattr(pricing.confidence, "value", pricing.confidence) if pricing is not None else "",
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

        new_price = _normalize_tail8_price(new_price)
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
            refreshed = _refresh_imported_detail(svc, item)
            record_source = refreshed or detail
            settle = _resolve_final_settle_price(svc, detail.product_id, new_price, refreshed)
            history_db.record(PriceChangeRecord(
                id=None,
                timestamp=datetime.datetime.now(),
                product_id=detail.product_id,
                qc_code=getattr(record_source, "qc_code", detail.qc_code) or item.qc_code,
                title=getattr(record_source, "title", detail.title),
                model=getattr(record_source, "model", detail.model),
                condition=getattr(record_source, "condition", detail.condition),
                capacity=getattr(record_source, "capacity", detail.capacity),
                color=getattr(record_source, "color", detail.color),
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
                settle_price=settle,
                suggested_price=new_price,
                suggested_settle_price=settle,
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
    login_state_cache: dict[str, tuple[bool, str]] = {}

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

        login_ok, login_reason = _account_login_state(svc, account.name, login_state_cache)
        if not login_ok:
            reason_text = str(login_reason or "登录状态过期，请重新登录").strip()
            imported_store.update_item(
                item.product_id,
                reprice_ok=False,
                reprice_msg=f"账号登录失效：{reason_text}",
                op_status="失败",
                op_message=f"滞销任务跳过：账号登录失效（{reason_text}）",
            )
            _log(f"[{account.name}] [{item.qc_code or item.product_id}] 跳过: 账号登录失效（{reason_text}）", on_progress)
            fail += 1
            continue

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
        )
        pipeline = run_reprice_pipeline(
            work_item,
            sold_cache,
            rule_engine,
            apply_rules=True,
        )
        pricing = pipeline["pricing"]
        decision = pipeline["decision"]

        if stale_days >= stage2:
            base_price = decision.get("system_price") or getattr(pricing, "conservative_price", None) or detail.current_price
            stage_price = _normalize_tail8_price(base_price * (1 - drop_pct / 100))
            decision = recalc_decision_with_final_price(work_item, decision, stage_price)
            stage_label = "阶段2"
        else:
            base_price = decision.get("system_price") or getattr(pricing, "conservative_price", None)
            if base_price is None or base_price >= detail.current_price:
                preview = _pricing_preview(pricing, rule_engine, work_item, decision)
                imported_store.update_item(
                    item.product_id,
                    pricing=pricing,
                    suggested_price=base_price,
                    new_price=None,
                    floor_price=pricing.floor_price,
                    suggested_settle_price=None,
                    confidence=getattr(pricing.confidence, "value", pricing.confidence),
                    rule_hit=decision.get("rule_hit", ""),
                    reprice_ok=None,
                    reprice_msg=f"保守价无下降空间｜{preview}",
                    op_status="跳过",
                    op_message=f"滞销任务跳过｜{preview}",
                )
                _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 跳过: 保守价无下降空间｜{preview}", on_progress)
                skip += 1
                continue
            decision = recalc_decision_with_final_price(work_item, decision, _normalize_tail8_price(base_price))
            stage_label = "阶段1"

        new_price = decision.get("final_price")
        preview = _pricing_preview(pricing, rule_engine, work_item, decision)

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
                rule_hit=decision.get("rule_hit", ""),
                reprice_ok=None,
                reprice_msg=f"{manual_reason}｜{preview}",
                op_status="待确认",
                op_message=f"{stage_label}自动降价已拦截，需人工确认｜{preview}",
            )
            _log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 待人工确认: {manual_reason}｜{preview}", on_progress)
            skip += 1
            continue

        if new_price is None or abs(new_price - detail.current_price) < 1:
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
            refreshed = _refresh_imported_detail(svc, item)
            record_source = refreshed or detail
            settle = _resolve_final_settle_price(svc, detail.product_id, new_price, refreshed)
            history_db.record(PriceChangeRecord(
                id=None,
                timestamp=datetime.datetime.now(),
                product_id=detail.product_id,
                qc_code=getattr(record_source, "qc_code", detail.qc_code) or item.qc_code,
                title=getattr(record_source, "title", detail.title),
                model=getattr(record_source, "model", detail.model),
                condition=getattr(record_source, "condition", detail.condition),
                capacity=getattr(record_source, "capacity", detail.capacity),
                color=getattr(record_source, "color", detail.color),
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
                settle_price=settle,
                suggested_price=new_price,
                suggested_settle_price=settle,
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
    history_db = get_history_db()
    accounts = {account.name: account for account in account_store.enabled_accounts()}
    items = imported_store.get_all()
    ok = fail = skip = 0

    def log(msg):
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    if not items:
        log("导入商品列表中没有可处理商品，跳过未上架自动上架")
        return {"ok": 0, "fail": 0, "skip": 0, "error": ""}

    services: dict[str, ImeiService] = {}
    login_state_cache: dict[str, tuple[bool, str]] = {}
    for item in items:
        skip_reason = _listing_skip_reason(item)
        if skip_reason:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=skip_reason,
                op_status="跳过",
                op_message=skip_reason,
            )
            skip += 1
            continue

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

        login_ok, login_reason = _account_login_state(svc, account.name, login_state_cache)
        if not login_ok:
            reason_text = str(login_reason or "登录状态过期，请重新登录").strip()
            imported_store.update_item(
                item.product_id,
                op_status="失败",
                op_message=f"自动上架跳过：账号登录失效（{reason_text}）",
                reprice_ok=False,
                reprice_msg=f"账号登录失效：{reason_text}",
            )
            log(f"[{account.name}] [{item.qc_code or item.product_id}] 跳过: 账号登录失效（{reason_text}）")
            fail += 1
            continue

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

        refreshed_listing_eligible = getattr(item, "import_source", "") in _allowed_listing_import_sources() and detail.status == ProductStatus.NOT_LISTED
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
            import_source=getattr(item, "import_source", ""),
            listing_eligible=refreshed_listing_eligible,
        )

        refreshed_item = imported_store.get(item.product_id) or item
        refreshed_skip_reason = _listing_skip_reason(refreshed_item)
        if refreshed_skip_reason:
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=refreshed_skip_reason,
                op_status="跳过",
                op_message=refreshed_skip_reason,
            )
            skip += 1
            continue

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
            import_source=getattr(item, "import_source", ""),
            listing_eligible=refreshed_listing_eligible,
        )
        pipeline = run_reprice_pipeline(
            work_item,
            sold_cache,
            rule_engine,
            apply_rules=True,
            settle_price_estimator=lambda price, product_id=detail.product_id: _estimate_suggested_settle_price(svc, product_id, price),
        )
        pricing = pipeline["pricing"]
        decision = pipeline["decision"]
        preview = pipeline["preview"]
        price = pipeline["suggested_price"]

        if price is None:
            imported_store.update_item(
                item.product_id,
                pricing=pricing,
                suggested_price=None,
                new_price=None,
                floor_price=pricing.floor_price,
                suggested_settle_price=None,
                confidence=getattr(pricing.confidence, "value", pricing.confidence),
                rule_hit=decision.get("rule_hit", ""),
                op_status="跳过",
                op_message=f"无定价依据，未执行自动上架｜{preview}",
                reprice_ok=None,
                reprice_msg=f"无定价依据｜{preview}",
            )
            skip += 1
            continue

        price = _normalize_tail8_price(price)
        if decision.get("needs_manual_review"):
            imported_store.update_item(
                item.product_id,
                pricing=pricing,
                suggested_price=price,
                new_price=price,
                floor_price=pricing.floor_price,
                suggested_settle_price=decision.get("final_settle_price"),
                confidence=getattr(pricing.confidence, "value", pricing.confidence),
                rule_hit=decision.get("rule_hit", ""),
                op_status="待确认",
                op_message=f"自动上架已拦截，需人工确认｜{preview}",
                reprice_ok=None,
                reprice_msg=f"{decision.get('manual_review_reason') or '建议价低于成本，需人工确认'}｜{preview}",
            )
            log(f"[{account.name}] [{detail.qc_code or item.qc_code}] 待人工确认: {preview}")
            skip += 1
            continue

        suggested_settle = pipeline.get("suggested_settle_price")
        imported_store.update_item(
            item.product_id,
            pricing=pricing,
            suggested_price=price,
            new_price=price,
            floor_price=pricing.floor_price,
            suggested_settle_price=suggested_settle,
            confidence=getattr(pricing.confidence, "value", pricing.confidence),
            rule_hit=decision.get("rule_hit", ""),
        )
        success, msg = svc.list_product(detail.product_id, price, detail.qc_code)
        if success:
            ok += 1
            settle = PricingEngine.calc_settle_price(price)
            refreshed = _refresh_imported_detail(svc, item) or detail
            record_source = refreshed or detail
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
                listing_eligible=getattr(refreshed, "status", None) == ProductStatus.NOT_LISTED,
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
                qc_code=getattr(record_source, "qc_code", detail.qc_code) or item.qc_code,
                title=getattr(record_source, "title", detail.title),
                model=getattr(record_source, "model", detail.model),
                condition=getattr(record_source, "condition", detail.condition),
                capacity=getattr(record_source, "capacity", detail.capacity),
                color=getattr(record_source, "color", detail.color),
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
