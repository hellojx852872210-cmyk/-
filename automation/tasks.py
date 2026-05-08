# -*- coding: utf-8 -*-
"""
自动化任务实现
每个任务函数由 scheduler 定期调用，或通过企业微信指令触发
"""
from __future__ import annotations

import datetime
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

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

ERP_MATCH_BATCH_SIZE = 30
ERP_FALLBACK_LOOKUP_BUDGET = 120

MANUAL_REVIEW_STATE_PENDING = "pending"
MANUAL_REVIEW_STATE_ACCEPTED = "accepted"
MANUAL_REVIEW_STATE_REJECTED = "rejected"
MANUAL_REVIEW_STATE_REJECTED_IGNORED = "rejected_ignored"
MANUAL_REVIEW_ACTION_ACCEPT = "accept"
MANUAL_REVIEW_ACTION_REJECT = "reject"
MANUAL_REVIEW_ACTION_REJECT_AND_IGNORE = "reject_and_ignore"

STATUS_NOTIFY_DEDUPE_KEY = "sales_status_notify_dedupe_v1"
STATUS_NOTIFY_DEDUPE_TTL_HOURS = 72
STATUS_NOTIFY_DEDUPE_MAX_ENTRIES = 2000
STATUS_NOTIFY_MAX_LINES = 20


def _emit_progress_event(
    on_progress_event: Optional[Callable[[dict[str, Any]], None]],
    *,
    stage: str,
    current: int,
    total: int,
    message: str,
) -> None:
    if on_progress_event is None:
        return
    try:
        on_progress_event(
            {
                "stage": str(stage),
                "current": max(int(current or 0), 0),
                "total": max(int(total or 0), 0),
                "message": str(message or ""),
            }
        )
    except Exception:
        return


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


def _fetch_official_reference_for_item(svc: ImeiService, item: BatchItem | object) -> Optional[dict]:
    category_id = int(getattr(item, "category_id", 0) or 0)
    brand_id = int(getattr(item, "brand_id", 0) or 0)
    model_id = int(getattr(item, "model_id", 0) or 0)
    product_params = str(getattr(item, "product_params", "") or "").strip()
    condition = str(getattr(item, "condition", "") or "")
    return svc.query_official_reference_price(
        category_id=category_id,
        brand_id=brand_id,
        model_id=model_id,
        product_params=product_params,
        condition=condition,
    )


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


def _status_label(value) -> str:
    status_obj = getattr(value, "status", value)
    label = str(getattr(status_obj, "label", "") or "").strip()
    if label:
        return label
    raw = str(getattr(status_obj, "value", status_obj) or "").strip()
    mapping = {
        ProductStatus.ON_SALE.value: "在售",
        ProductStatus.NOT_LISTED.value: "未上架",
        ProductStatus.SOLD.value: "已售",
        ProductStatus.OFF_SALE.value: "已下架",
        ProductStatus.IN_QC.value: "质检中",
        ProductStatus.UNKNOWN.value: "未知",
        "": "未知",
        "-": "未知",
    }
    return mapping.get(raw, raw or "未知")


def _send_sales_status_report(notifier, wx_app_client, report: str) -> bool:
    if not report:
        return False
    if notifier is not None and hasattr(notifier, "send_text"):
        try:
            if notifier.send_text(report):
                return True
        except Exception:
            pass
    if wx_app_client is not None and hasattr(wx_app_client, "broadcast_text"):
        try:
            return bool(wx_app_client.broadcast_text(report))
        except Exception:
            return False
    return False


def _status_notify_dedupe_key(product_id: str, old_status: str, new_status: str) -> str:
    return f"{str(product_id or '').strip()}|{str(old_status or '').strip()}->{str(new_status or '').strip()}"


def _load_status_notify_dedupe_state(now: datetime.datetime) -> dict[str, datetime.datetime]:
    raw = cfg.get(STATUS_NOTIFY_DEDUPE_KEY, {})
    if not isinstance(raw, dict):
        return {}
    expire_before = now - datetime.timedelta(hours=STATUS_NOTIFY_DEDUPE_TTL_HOURS)
    parsed: dict[str, datetime.datetime] = {}
    for key, value in raw.items():
        if not key:
            continue
        try:
            ts = datetime.datetime.fromisoformat(str(value))
        except Exception:
            continue
        if ts >= expire_before:
            parsed[str(key)] = ts
    return parsed


def _save_status_notify_dedupe_state(state: dict[str, datetime.datetime]) -> None:
    if not state:
        cfg.set(STATUS_NOTIFY_DEDUPE_KEY, {})
        return
    ordered = sorted(state.items(), key=lambda kv: kv[1], reverse=True)[:STATUS_NOTIFY_DEDUPE_MAX_ENTRIES]
    cfg.set(STATUS_NOTIFY_DEDUPE_KEY, {key: ts.isoformat() for key, ts in ordered})


def _build_status_notify_report(records: list[dict]) -> str:
    now = datetime.datetime.now()
    title = f"📣 导入商品状态播报 {now.strftime('%m/%d %H:%M')}"
    lines = [title, f"状态变更：{len(records)} 条"]
    for index, record in enumerate(records[:STATUS_NOTIFY_MAX_LINES], start=1):
        account = str(record.get("account_name") or "-")
        mark = str(record.get("qc_code") or record.get("product_id") or "-")
        old_status = str(record.get("old_status") or "未知")
        new_status = str(record.get("new_status") or "未知")
        detail = str(record.get("status_detail") or "").strip()
        line = f"{index}. [{account}] [{mark}] {old_status}→{new_status}"
        if detail:
            line += f"（{detail}）"
        lines.append(line)
    if len(records) > STATUS_NOTIFY_MAX_LINES:
        lines.append(f"… 其余 {len(records) - STATUS_NOTIFY_MAX_LINES} 条省略")
    return "\n".join(lines)


def _iter_actionable_items(imported_store: BatchItemStore) -> list[BatchItem]:
    items = imported_store.get_all()
    return [
        item for item in items
        if not getattr(item, "ignored", False)
        and getattr(item, "selected", True)
        and getattr(item, "manual_review_state", "none") not in {MANUAL_REVIEW_STATE_PENDING, MANUAL_REVIEW_STATE_REJECTED, MANUAL_REVIEW_STATE_REJECTED_IGNORED}
        and str(getattr(item, "op_status", "") or "").strip() != "待确认"
    ]


def _should_skip_status_for_auto_reprice(status) -> bool:
    return status in {ProductStatus.OFF_SALE, ProductStatus.SOLD, ProductStatus.NOT_LISTED, ProductStatus.IN_QC}


def _summarize_skip_bucket(skip_bucket: dict[str, int], reason: str) -> None:
    skip_bucket[reason] = int(skip_bucket.get(reason, 0) or 0) + 1


def _format_short_reprice_log(item, current_price: float, final_price: float, preview: str) -> str:
    mark = item.qc_code or item.product_id
    delta = final_price - current_price
    return f"[{item.account_name}] [{mark}] {current_price:.0f}->{final_price:.0f} ({delta:+.0f}) | {preview[:80]}"


def _append_persisted_sample(
    samples: list[dict[str, Any]],
    *,
    task: str,
    account: str,
    item: str,
    old_price: float,
    new_price: float,
    trigger: str,
    limit: int = 20,
) -> None:
    if len(samples) >= max(int(limit or 0), 1):
        return
    samples.append(
        {
            "task": task,
            "account": account,
            "item": item,
            "old_price": round(float(old_price or 0.0), 0),
            "new_price": round(float(new_price or 0.0), 0),
            "diff": round(float(new_price or 0.0) - float(old_price or 0.0), 0),
            "trigger": trigger,
        }
    )


def _manual_review_context_fields(
    decision: Optional[dict],
    *,
    source: str,
    target_action: str,
    target_price: Optional[float],
) -> dict:
    data = decision or {}
    return {
        "needs_manual_review": bool(data.get("needs_manual_review")),
        "manual_review_reason": str(data.get("manual_review_reason") or ""),
        "manual_review_state": MANUAL_REVIEW_STATE_PENDING if data.get("needs_manual_review") else "none",
        "manual_review_source": source if data.get("needs_manual_review") else "",
        "manual_review_target_action": target_action if data.get("needs_manual_review") else "",
        "manual_review_target_price": target_price if data.get("needs_manual_review") else None,
    }


def _manual_review_reset_fields() -> dict:
    return {
        "needs_manual_review": False,
        "manual_review_reason": "",
        "manual_review_state": "none",
        "manual_review_source": "",
        "manual_review_target_action": "",
        "manual_review_target_price": None,
    }


def _manual_review_source_label(source: str) -> str:
    mapping = {
        "auto_reprice": "自动调价",
        "stale_drop": "滞销降价",
        "auto_list": "自动上架",
    }
    return mapping.get(str(source or "").strip(), "人工确认")


def _apply_manual_review_decision_status(
    imported_store: BatchItemStore,
    item: BatchItem,
    *,
    state: str,
    op_status: str,
    op_message: str,
    ignored: Optional[bool] = None,
) -> None:
    payload: dict[str, Any] = {
        **_manual_review_reset_fields(),
        "manual_review_state": state,
        "reprice_ok": None,
        "op_status": op_status,
        "op_message": op_message,
        "reprice_msg": op_message,
    }
    if ignored is not None:
        payload["ignored"] = bool(ignored)
    imported_store.update_item(item.product_id, **payload)


def apply_manual_review_decision(
    account_store: AccountStore,
    imported_store: BatchItemStore,
    product_id: str,
    action: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    item = imported_store.get(str(product_id or ""))
    if item is None:
        return False, "未找到商品"

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {
        MANUAL_REVIEW_ACTION_ACCEPT,
        MANUAL_REVIEW_ACTION_REJECT,
        MANUAL_REVIEW_ACTION_REJECT_AND_IGNORE,
    }:
        return False, f"不支持的人工确认动作: {action}"

    if normalized_action == MANUAL_REVIEW_ACTION_REJECT:
        _apply_manual_review_decision_status(
            imported_store,
            item,
            state=MANUAL_REVIEW_STATE_REJECTED,
            op_status="已永久拒绝",
            op_message="人工确认：永久拒绝",
            ignored=False,
        )
        return True, "已永久拒绝"

    if normalized_action == MANUAL_REVIEW_ACTION_REJECT_AND_IGNORE:
        _apply_manual_review_decision_status(
            imported_store,
            item,
            state=MANUAL_REVIEW_STATE_REJECTED_IGNORED,
            op_status="已拒绝并移入不处理区",
            op_message="人工确认：永久拒绝并移入不处理区",
            ignored=True,
        )
        return True, "已拒绝并移入不处理区"

    account_name = str(getattr(item, "account_name", "") or "").strip()
    accounts = {account.name: account for account in account_store.enabled_accounts()}
    account = accounts.get(account_name)
    if account is None:
        return False, "账号不存在或未启用"

    svc = ImeiService(account.name, account.cookie)
    login_ok, login_reason = _account_login_state(svc, account.name, {})
    if not login_ok:
        reason_text = str(login_reason or "登录状态过期，请重新登录").strip()
        return False, f"账号登录失效：{reason_text}"

    target_action = str(getattr(item, "manual_review_target_action", "") or "").strip() or "change_price"
    source = str(getattr(item, "manual_review_source", "") or "").strip()
    source_label = _manual_review_source_label(source)
    accepted_review_fields = {
        **_manual_review_reset_fields(),
        "manual_review_state": MANUAL_REVIEW_STATE_ACCEPTED,
    }
    target_price = getattr(item, "manual_review_target_price", None)
    if target_price is None:
        target_price = getattr(item, "new_price", None)
    if target_price is None:
        target_price = getattr(item, "suggested_price", None)
    if target_action in {"change_price", "list_product"} and target_price is None:
        return False, "缺少待执行价格，无法接受"

    detail = _refresh_imported_detail(svc, item)
    if detail is None and target_action == "change_price":
        return False, "未找到商品详情，无法执行改价"

    if target_action == "change_price":
        success, msg = svc.change_price(detail, float(target_price))
        if not success:
            return False, msg or "人工确认改价失败"
        refreshed = _refresh_imported_detail(svc, item) or detail
        settle = _resolve_final_settle_price(svc, item.product_id, float(target_price), refreshed)
        imported_store.update_item(
            item.product_id,
            qc_code=getattr(refreshed, "qc_code", item.qc_code) or item.qc_code,
            title=getattr(refreshed, "title", item.title),
            model=getattr(refreshed, "model", item.model),
            condition=getattr(refreshed, "condition", item.condition),
            capacity=getattr(refreshed, "capacity", item.capacity),
            color=getattr(refreshed, "color", item.color),
            current_price=getattr(refreshed, "current_price", float(target_price)),
            settle_price=settle,
            suggested_price=float(target_price),
            suggested_settle_price=settle,
            listed_time=getattr(refreshed, "listed_time", item.listed_time),
            status=getattr(refreshed, "status", item.status),
            status_detail=_status_detail_from_obj(refreshed or item),
            new_price=float(target_price),
            reprice_ok=True,
            op_status="已改价",
            op_message=f"人工确认通过（{source_label}）",
            reprice_msg=msg or "人工确认改价成功",
            ignored=False,
            **accepted_review_fields,
        )
        _log(f"[{account.name}] [{item.qc_code or item.product_id}] 人工确认改价成功: {target_price}", on_progress)
        return True, msg or "人工确认改价成功"

    if target_action == "list_product":
        success, msg = svc.list_product(str(item.product_id), float(target_price), str(item.qc_code or ""))
        if not success:
            return False, msg or "人工确认上架失败"
        refreshed = _refresh_imported_detail(svc, item) or item
        settle = _resolve_final_settle_price(svc, item.product_id, float(target_price), refreshed)
        imported_store.update_item(
            item.product_id,
            qc_code=getattr(refreshed, "qc_code", item.qc_code) or item.qc_code,
            title=getattr(refreshed, "title", item.title),
            model=getattr(refreshed, "model", item.model),
            condition=getattr(refreshed, "condition", item.condition),
            capacity=getattr(refreshed, "capacity", item.capacity),
            color=getattr(refreshed, "color", item.color),
            current_price=getattr(refreshed, "current_price", float(target_price)),
            settle_price=getattr(refreshed, "settle_price", settle) or settle,
            suggested_price=float(target_price),
            suggested_settle_price=getattr(refreshed, "settle_price", settle) or settle,
            listed_time=getattr(refreshed, "listed_time", item.listed_time),
            status=getattr(refreshed, "status", item.status),
            status_detail=_status_detail_from_obj(refreshed or item),
            new_price=float(target_price),
            listing_eligible=getattr(refreshed, "status", None) == ProductStatus.NOT_LISTED,
            reprice_ok=True,
            op_status="已上架",
            op_message=f"人工确认通过（{source_label}）",
            reprice_msg=msg or "人工确认上架成功",
            ignored=False,
            **accepted_review_fields,
        )
        _log(f"[{account.name}] [{item.qc_code or item.product_id}] 人工确认上架成功: {target_price}", on_progress)
        return True, msg or "人工确认上架成功"

    return False, f"不支持的待执行动作: {target_action}"


def apply_manual_review_batch_decision(
    account_store: AccountStore,
    imported_store: BatchItemStore,
    product_ids: list[str],
    action: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    ok = 0
    fail = 0
    fail_messages: list[str] = []
    for pid in product_ids:
        success, message = apply_manual_review_decision(
            account_store,
            imported_store,
            pid,
            action,
            on_progress=on_progress,
        )
        if success:
            ok += 1
        else:
            fail += 1
            fail_messages.append(f"{pid}: {message}")
    return {
        "ok": ok,
        "fail": fail,
        "fail_messages": fail_messages,
    }


def update_imported_items_selected(
    imported_store: BatchItemStore,
    product_ids: list[str],
    checked: bool,
) -> int:
    count = 0
    for pid in product_ids:
        if imported_store.get(pid) is None:
            continue
        imported_store.update_item(pid, selected=checked)
        count += 1
    return count


def update_imported_items_ignored(
    imported_store: BatchItemStore,
    product_ids: list[str],
    ignored: bool,
) -> int:
    count = 0
    for pid in product_ids:
        if imported_store.get(pid) is None:
            continue
        imported_store.update_item(pid, ignored=ignored)
        count += 1
    return count


def update_imported_items_category(
    imported_store: BatchItemStore,
    product_ids: list[str],
    category: str,
) -> int:
    value = str(category or "").strip()
    count = 0
    for pid in product_ids:
        if imported_store.get(pid) is None:
            continue
        imported_store.update_item(pid, category=value)
        count += 1
    return count


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
    *,
    fallback_budget: Optional[list[int]] = None,
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
        if fallback_budget is not None and fallback_budget[0] <= 0:
            return None, f"单条兜底预算已耗尽；未找到 lookup code: {', '.join(codes)}"
        if fallback_budget is not None:
            fallback_budget[0] -= 1
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
    state = str(getattr(item, "manual_review_state", "") or "").strip().lower()
    if state in {MANUAL_REVIEW_STATE_REJECTED, MANUAL_REVIEW_STATE_REJECTED_IGNORED}:
        return "商品已永久拒绝"
    if state == MANUAL_REVIEW_STATE_PENDING or str(getattr(item, "op_status", "") or "").strip() == "待确认":
        return "商品待人工确认"
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

def _actionable_skip_reason(item: Optional[BatchItem]) -> str:
    if item is None:
        return "商品不存在"
    state = str(getattr(item, "manual_review_state", "") or "").strip().lower()
    if state in {MANUAL_REVIEW_STATE_REJECTED, MANUAL_REVIEW_STATE_REJECTED_IGNORED}:
        return "商品已永久拒绝"
    if state == MANUAL_REVIEW_STATE_PENDING or str(getattr(item, "op_status", "") or "").strip() == "待确认":
        return "商品待人工确认"
    if getattr(item, "ignored", False):
        return "商品在不处理区"
    if not getattr(item, "selected", True):
        return "商品未勾选参与自动化"
    return ""


def build_auto_preview_payload(
    item: Optional[BatchItem],
    sold_cache: SoldCache,
    rule_engine: RuleEngine,
    *,
    custom_mode: str,
    custom_value: float,
) -> dict[str, Any]:
    if item is None:
        return {
            "has_item": False,
            "pipeline": None,
            "module_preview": {
                "auto_reprice": "-",
                "stale_drop": "-",
                "auto_list": "-",
            },
        }
    pipeline = run_reprice_pipeline(
        item,
        sold_cache,
        rule_engine,
        custom_mode=custom_mode,
        custom_value=custom_value,
        apply_rules=True,
    )
    decision = pipeline.get("decision") or {}
    module_preview = build_next_run_module_preview(
        item,
        decision,
        suggested_price=pipeline.get("suggested_price"),
    )
    return {
        "has_item": True,
        "item": item,
        "pipeline": pipeline,
        "module_preview": module_preview,
    }


def build_auto_preview_batch_rows(
    items: list[BatchItem],
    sold_cache: SoldCache,
    rule_engine: RuleEngine,
    *,
    custom_mode: str,
    custom_value: float,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    safe_limit = max(int(limit or 0), 0)
    if safe_limit == 0:
        return rows
    for item in items[:safe_limit]:
        pipeline = run_reprice_pipeline(
            item,
            sold_cache,
            rule_engine,
            custom_mode=custom_mode,
            custom_value=custom_value,
            apply_rules=True,
        )
        decision = pipeline.get("decision") or {}
        current_price = getattr(item, "current_price", None)
        final_price = pipeline.get("suggested_price")
        delta = None
        if final_price is not None and current_price is not None:
            delta = float(final_price) - float(current_price)
        rows.append(
            {
                "item": item,
                "pipeline": pipeline,
                "decision": decision,
                "current_price": current_price,
                "system_price": decision.get("system_price"),
                "final_price": final_price,
                "delta": delta,
            }
        )
    return rows


def match_imported_items_preview(
    imported_store: BatchItemStore,
    ids: list[str],
    sold_cache: SoldCache,
    rule_engine: RuleEngine,
) -> dict[str, int]:
    matched = 0
    skipped = 0
    for pid in ids:
        item = imported_store.get(pid)
        if item is None:
            continue
        pipeline = run_reprice_pipeline(
            item,
            sold_cache,
            rule_engine,
            apply_rules=True,
        )
        pricing = pipeline.get("pricing")
        decision = pipeline.get("decision") or {}
        preview = pipeline.get("preview") or "—"
        suggested = pipeline.get("suggested_price")
        settle = pipeline.get("suggested_settle_price")
        if suggested is None:
            imported_store.update_item(
                pid,
                pricing=pricing,
                suggested_price=None,
                suggested_settle_price=None,
                op_status="跳过",
                op_message=f"无定价依据｜{preview}",
                reprice_ok=None,
            )
            skipped += 1
            continue
        imported_store.update_item(
            pid,
            pricing=pricing,
            suggested_price=round(float(suggested), 0),
            suggested_settle_price=settle,
            confidence=getattr(getattr(pricing, "confidence", None), "value", getattr(pricing, "confidence", "")),
            rule_hit=decision.get("rule_hit", ""),
            op_status="已匹配",
            op_message=f"建议价预览已刷新｜{preview}",
            reprice_ok=None,
        )
        matched += 1
    return {"matched": matched, "skipped": skipped}


def build_next_run_module_preview(
    item: Optional[BatchItem],
    decision: Optional[dict],
    *,
    suggested_price: Optional[float],
) -> dict[str, str]:
    result = {
        "auto_reprice": "-",
        "stale_drop": "-",
        "auto_list": "-",
    }
    if item is None:
        return result

    actionable_skip = _actionable_skip_reason(item)
    if actionable_skip:
        result["auto_reprice"] = f"跳过：{actionable_skip}"
        result["stale_drop"] = f"跳过：{actionable_skip}"
    else:
        current_price = float(getattr(item, "current_price", 0.0) or 0.0)
        if suggested_price is None:
            result["auto_reprice"] = "跳过：无系统建议价"
            result["stale_drop"] = "跳过：无系统建议价"
        else:
            if decision and decision.get("needs_manual_review"):
                reason = str(decision.get("manual_review_reason") or "需人工确认")
                result["auto_reprice"] = f"待确认：{reason}"
            elif abs(float(suggested_price) - current_price) < 1:
                result["auto_reprice"] = "跳过：建议价与当前价差异小于 1 元"
            else:
                result["auto_reprice"] = f"预计改价到 ¥{float(suggested_price):,.0f}"

            status = getattr(item, "status", None)
            if status != ProductStatus.ON_SALE:
                status_label = getattr(status, "label", "") or str(getattr(status, "value", status) or "未知")
                result["stale_drop"] = f"跳过：当前状态 {status_label}"
            elif not getattr(item, "listed_time", None):
                result["stale_drop"] = "跳过：缺少上架时间"
            else:
                stale_days = max((datetime.datetime.now() - item.listed_time).days, 0)
                stage1 = cfg.stale_stage1_days
                stage2 = cfg.stale_stage2_days
                drop_pct = cfg.stale_stage2_drop_pct
                if stale_days < stage1:
                    result["stale_drop"] = f"跳过：在架 {stale_days} 天，未达阈值 {stage1}"
                else:
                    if stale_days >= stage2:
                        base_price = decision.get("system_price") if isinstance(decision, dict) else suggested_price
                        stage_price = _normalize_tail8_price((base_price or current_price) * (1 - drop_pct / 100))
                        stage_decision = recalc_decision_with_final_price(item, decision or {}, stage_price)
                        stage_label = "阶段2"
                    else:
                        stage_price = _normalize_tail8_price(((decision or {}).get("system_price") if isinstance(decision, dict) else suggested_price) or suggested_price)
                        stage_decision = recalc_decision_with_final_price(item, decision or {}, stage_price)
                        stage_label = "阶段1"
                    stage_final_price = stage_decision.get("final_price")
                    if stage_final_price is None or abs(float(stage_final_price) - current_price) < 1:
                        result["stale_drop"] = f"{stage_label}：跳过（无有效降价空间）"
                    elif stage_decision.get("needs_manual_review"):
                        reason = str(stage_decision.get("manual_review_reason") or "需人工确认")
                        result["stale_drop"] = f"{stage_label}：待确认（{reason}）"
                    else:
                        result["stale_drop"] = f"{stage_label}：预计改价到 ¥{float(stage_final_price):,.0f}"

    listing_reason = _listing_skip_reason(item)
    if listing_reason:
        result["auto_list"] = f"跳过：{listing_reason}"
    elif suggested_price is None:
        result["auto_list"] = "跳过：无系统建议价"
    elif decision and decision.get("needs_manual_review"):
        reason = str(decision.get("manual_review_reason") or "需人工确认")
        result["auto_list"] = f"待确认：{reason}"
    else:
        result["auto_list"] = f"预计按 ¥{float(suggested_price):,.0f} 自动上架"
    return result


def _is_manual_review_pending(item: Optional[BatchItem]) -> bool:
    if item is None:
        return False
    state = str(getattr(item, "manual_review_state", "") or "").strip().lower()
    if state == MANUAL_REVIEW_STATE_PENDING:
        return True
    return str(getattr(item, "op_status", "") or "").strip() == "待确认"


def _manual_review_fields(
    decision: Optional[dict],
    *,
    source: str,
    target_action: str,
    target_price: Optional[float],
) -> dict:
    return _manual_review_context_fields(
        decision,
        source=source,
        target_action=target_action,
        target_price=target_price,
    )


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
    on_progress_event: Optional[Callable[[dict[str, Any]], None]] = None,
    match_total: int = 0,
    match_completed: int = 0,
    batch_size: int = ERP_MATCH_BATCH_SIZE,
    fallback_budget: int = ERP_FALLBACK_LOOKUP_BUDGET,
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

    safe_batch_size = max(1, int(batch_size or ERP_MATCH_BATCH_SIZE))
    try:
        details, missing = svc.fetch_by_codes(lookup_codes, batch_size=safe_batch_size)
        _log(
            f"[ERP_SYNC:{run_id}] [{account.name}] 批量匹配返回：明细 {len(details)} 件，缺失code {len(missing or [])} 个（batch={safe_batch_size}）",
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
    fallback_budget_ref = [max(0, int(fallback_budget or 0))]

    for index, erp_item in enumerate(erp_items, start=1):
        if index == 1 or index == len(erp_items) or index % progress_step == 0:
            _log(
                f"[ERP_SYNC:{run_id}] [{account.name}] 正在整理匹配结果 {index}/{len(erp_items)}，"
                f"当前已匹配 {len(matched)}，待确认 {len(unresolved)}",
                on_progress,
            )

        detail, reason = _match_detail_for_erp_item(
            svc,
            erp_item,
            detail_by_code,
            missing_codes=missing_codes,
            lookup_cache=lookup_cache,
            fallback_budget=fallback_budget_ref,
        )
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

        if match_total > 0 and (
            index == 1 or index == len(erp_items) or index % progress_step == 0
        ):
            progress_current = min(match_completed + index, match_total)
            _emit_progress_event(
                on_progress_event,
                stage="match",
                current=progress_current,
                total=match_total,
                message=f"[{account.name}] 匹配中 {progress_current}/{match_total}（成功 {len(matched)}，待确认 {len(unresolved)}）",
            )

    _log(
        f"[ERP_SYNC:{run_id}] [{account.name}] 账号匹配结束：匹配 {len(matched)} 件，未匹配 {len(unresolved)} 件，"
        f"detail_by_code {len(detail_by_code)} 个，missing_code {len(missing_codes)} 个，"
        f"fallback剩余额度 {fallback_budget_ref[0]}",
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
def task_erp_sync(
    erp_config: ErpConfig,
    cost_map: CostPriceMap,
    account_store: AccountStore,
    imported_store: Optional[BatchItemStore] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    on_progress_event: Optional[Callable[[dict[str, Any]], None]] = None,
    sold_cache: Optional[SoldCache] = None,
    sold_sync_days: int = 30,
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

        login_state_cache: dict[str, tuple[bool, str]] = {}
        cookie_failed_accounts: list[str] = []
        sales_sync_errors: list[str] = []
        sales_synced_accounts = 0
        sales_synced_added = 0
        sales_synced_updated = 0
        active_accounts: list = []
        sold_days = max(int(sold_sync_days or 0), 0)
        sold_since = datetime.datetime.now() - datetime.timedelta(days=sold_days) if sold_days else None

        if accounts:
            _emit_progress_event(
                on_progress_event,
                stage="prepare",
                current=0,
                total=len(accounts),
                message=f"账号预检 0/{len(accounts)}（Cookie校验+成交同步）",
            )
        for index, account in enumerate(accounts, start=1):
            svc = ImeiService(account.name, account.cookie)
            ok, reason = _account_login_state(svc, account.name, login_state_cache)
            if not ok:
                fail_msg = f"{account.name}: {reason or 'Cookie 无效'}"
                cookie_failed_accounts.append(fail_msg)
                _log(f"[ERP_SYNC:{run_id}] [{account.name}] Cookie 校验失败，已跳过该账号：{reason or '未知原因'}", on_progress)
                _emit_progress_event(
                    on_progress_event,
                    stage="prepare",
                    current=index,
                    total=len(accounts),
                    message=f"账号预检 {index}/{len(accounts)}（通过 {len(active_accounts)}，失败 {len(cookie_failed_accounts)}）",
                )
                continue

            active_accounts.append(account)
            if sold_cache is not None:
                try:
                    records = DataFetcher(account.name, account.cookie).fetch_all_sold(since=sold_since)
                    added, updated = sold_cache.upsert(records)
                    sales_synced_accounts += 1
                    sales_synced_added += int(added or 0)
                    sales_synced_updated += int(updated or 0)
                    _log(
                        f"[ERP_SYNC:{run_id}] [{account.name}] 成交同步完成（近 {sold_days} 天）：新增 {added}，更新 {updated}",
                        on_progress,
                    )
                except Exception as exc:
                    err = f"{account.name}: 成交同步失败: {exc}"
                    sales_sync_errors.append(err)
                    _log(f"[ERP_SYNC:{run_id}] {err}", on_progress)
            _emit_progress_event(
                on_progress_event,
                stage="prepare",
                current=index,
                total=len(accounts),
                message=f"账号预检 {index}/{len(accounts)}（通过 {len(active_accounts)}，失败 {len(cookie_failed_accounts)}）",
            )

        _log(f"[ERP_SYNC:{run_id}] 开始从爱管机 ERP 拉取商品...", on_progress)
        _emit_progress_event(
            on_progress_event,
            stage="fetch",
            current=0,
            total=2,
            message="ERP 拉取中 0/2（在售/在库并行）",
        )

        def _fetch_erp_channel(label: str, method_name: str):
            channel_fetcher = ErpFetcher(erp_config)
            channel_fetcher.check_and_refresh_token()
            fetch_method = getattr(channel_fetcher, method_name)
            return fetch_method()

        fetch_results: dict[str, list] = {}
        fetch_errors: list[str] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_map = {
                executor.submit(_fetch_erp_channel, "在售", "fetch_on_sale_items"): "在售",
                executor.submit(_fetch_erp_channel, "在库", "fetch_in_stock_items"): "在库",
            }
            completed_fetch = 0
            for future in as_completed(future_map):
                label = future_map[future]
                try:
                    items = future.result()
                except Exception as exc:
                    items = []
                    err_msg = f"ERP {label}拉取失败：{exc}"
                    fetch_errors.append(err_msg)
                    _log(f"[ERP_SYNC:{run_id}] {err_msg}", on_progress)
                fetch_results[label] = items
                completed_fetch += 1
                _emit_progress_event(
                    on_progress_event,
                    stage="fetch",
                    current=completed_fetch,
                    total=2,
                    message=f"ERP 拉取中 {completed_fetch}/2（{label} {len(items)} 件）",
                )
                _log(f"[ERP_SYNC:{run_id}] ERP {label}拉取完成：{len(items)} 件", on_progress)

        on_sale_erp = fetch_results.get("在售", [])
        in_stock_erp = fetch_results.get("在库", [])
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
            if fetch_errors:
                summary += f"（ERP 拉取异常：{'；'.join(fetch_errors)}）"
            _log(f"[ERP_SYNC:{run_id}] {summary}", on_progress)
            _emit_progress_event(
                on_progress_event,
                stage="done",
                current=1,
                total=1,
                message=summary,
            )
            return {
                "items": [],
                "summary": summary,
                "synced": len(cost_data),
                "matched": 0,
                "missed": len(all_erp),
                "error": "；".join(fetch_errors),
            }

        if not active_accounts:
            summary = f"同步成本价 {len(cost_data)} 条，Cookie 校验后无可用账号，未执行导入"
            if imported_store is not None:
                summary += f"，商品管理保留现有 {existing_count_before} 件"
            if cookie_failed_accounts:
                summary += f"（Cookie 失败：{'；'.join(cookie_failed_accounts)}）"
            if sales_sync_errors:
                summary += f"（成交同步异常：{'；'.join(sales_sync_errors)}）"
            if fetch_errors:
                summary += f"（ERP 拉取异常：{'；'.join(fetch_errors)}）"
            _log(f"[ERP_SYNC:{run_id}] {summary}", on_progress)
            _emit_progress_event(
                on_progress_event,
                stage="done",
                current=1,
                total=1,
                message=summary,
            )
            return {
                "items": [],
                "summary": summary,
                "synced": len(cost_data),
                "matched": 0,
                "missed": len(all_erp),
                "error": "；".join(cookie_failed_accounts + sales_sync_errors + fetch_errors),
            }

        matched_items: list[BatchItem] = []
        added_product_ids: set[str] = set()
        missed = 0
        error_accounts: list[str] = []
        matched_cost_from_map = 0
        matched_cost_from_erp = 0
        matched_missing_cost = 0
        total_items = len(all_erp)
        progress_step = 10 if total_items >= 100 else 5 if total_items >= 30 else 1

        _log(f"[ERP_SYNC:{run_id}] 开始批量匹配 ERP 商品到转转，共 {total_items} 件，账号 {len(active_accounts)} 个...", on_progress)
        _emit_progress_event(
            on_progress_event,
            stage="match",
            current=0,
            total=max(total_items, 1),
            message=f"开始匹配 0/{total_items}",
        )

        unmatched_items = list(all_erp)
        for account in active_accounts:
            if not unmatched_items:
                break
            _log(f"[ERP_SYNC:{run_id}] [{account.name}] 准备匹配：当前待匹配 ERP 商品 {len(unmatched_items)} 件", on_progress)
            match_completed = total_items - len(unmatched_items)
            matched_pairs, unresolved, had_error = _match_erp_items_for_account(
                account,
                unmatched_items,
                on_progress,
                run_id=run_id,
                on_progress_event=on_progress_event,
                match_total=total_items,
                match_completed=match_completed,
            )
            if had_error:
                error_accounts.append(account.name)
            for erp_item, matched_item in matched_pairs:
                map_cost = cost_map.get(matched_item.product_id)
                if map_cost is not None and float(map_cost) > 0:
                    resolved_cost = float(map_cost)
                    matched_cost_from_map += 1
                else:
                    erp_cost = float(getattr(erp_item, "cost_price", 0.0) or 0.0)
                    if erp_cost > 0:
                        resolved_cost = erp_cost
                        matched_cost_from_erp += 1
                    else:
                        resolved_cost = 0.0
                        matched_missing_cost += 1
                matched_item.cost_price = resolved_cost
                if matched_item.product_id not in added_product_ids:
                    matched_items.append(matched_item)
                    added_product_ids.add(matched_item.product_id)
            unmatched_items = unresolved
            completed = total_items - len(unmatched_items)
            _emit_progress_event(
                on_progress_event,
                stage="match",
                current=min(completed, max(total_items, 1)),
                total=max(total_items, 1),
                message=f"[{account.name}] 匹配完成 {completed}/{total_items}（成功 {len(matched_items)}，未找到 {len(unmatched_items)}）",
            )
            if total_items and (completed == total_items or completed % progress_step == 0):
                _log(
                    f"[ERP_SYNC:{run_id}] [{account.name}] 已匹配 {completed}/{total_items} 件 ERP 商品，成功 {len(matched_items)}，未找到 {len(unmatched_items)}...",
                    on_progress,
                )

        missed = len(unmatched_items)
        _log(
            f"[ERP_SYNC:{run_id}] 成本命中统计：cost_map {matched_cost_from_map} 件，"
            f"ERP 回退 {matched_cost_from_erp} 件，无成本 {matched_missing_cost} 件",
            on_progress,
        )
        updated_count = 0
        inserted_count = 0
        existing_count_after = existing_count_before
        write_total = max(len(matched_items), 1)
        write_current = 0
        _emit_progress_event(
            on_progress_event,
            stage="write",
            current=0,
            total=write_total,
            message=f"开始写入导入商品 0/{write_total}",
        )
        if imported_store is not None:
            existing_items = imported_store.get_all()
            existing_ids = {it.product_id for it in existing_items}
            new_items: list[BatchItem] = []
            write_step = 50 if len(matched_items) >= 200 else 20 if len(matched_items) >= 80 else 10 if len(matched_items) >= 20 else 1
            for index, matched_item in enumerate(matched_items, start=1):
                pid = matched_item.product_id
                if not pid:
                    write_current = index
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
                write_current = index
                if index == 1 or index == len(matched_items) or index % write_step == 0:
                    _emit_progress_event(
                        on_progress_event,
                        stage="write",
                        current=min(write_current, write_total),
                        total=write_total,
                        message=f"写入导入商品 {min(write_current, write_total)}/{write_total}（更新 {updated_count}，待新增 {len(new_items)}）",
                    )
            if new_items:
                imported_store.extend(new_items)
                inserted_count = len(new_items)
            existing_count_after = imported_store.count()
            _log(
                f"[ERP_SYNC:{run_id}] 商品管理写入完成：更新 {updated_count} 件，新增 {inserted_count} 件，"
                f"导入前 {existing_count_before} 件，导入后 {existing_count_after} 件",
                on_progress,
            )
            _emit_progress_event(
                on_progress_event,
                stage="write",
                current=write_total,
                total=write_total,
                message=f"写入完成（更新 {updated_count}，新增 {inserted_count}）",
            )
        else:
            _emit_progress_event(
                on_progress_event,
                stage="write",
                current=write_total,
                total=write_total,
                message="未传入 imported_store，跳过写入",
            )

        summary = f"同步成本价 {len(cost_data)} 条，匹配 {len(matched_items)} 件，未找到 {missed} 件"
        summary += f"，Cookie 通过 {len(active_accounts)}/{len(accounts)}"
        if sold_cache is not None:
            summary += (
                f"，成交同步账号 {sales_synced_accounts} 个"
                f"（新增 {sales_synced_added}，更新 {sales_synced_updated}）"
            )
        if imported_store is not None:
            summary += f"，已导入商品管理 {existing_count_after} 件"
        if error_accounts:
            summary += f"（以下店铺匹配接口报错已跳过：{', '.join(error_accounts)}）"
        if cookie_failed_accounts:
            summary += f"（Cookie 失败：{'；'.join(cookie_failed_accounts)}）"
        if sales_sync_errors:
            summary += f"（成交同步异常：{'；'.join(sales_sync_errors)}）"
        if fetch_errors:
            summary += f"（ERP 拉取异常：{'；'.join(fetch_errors)}）"
        _log(f"[ERP_SYNC:{run_id}] {summary}", on_progress)
        _emit_progress_event(
            on_progress_event,
            stage="done",
            current=1,
            total=1,
            message=summary,
        )
        error_messages: list[str] = []
        if error_accounts:
            error_messages.extend(f"{name}: 匹配接口报错" for name in error_accounts)
        if cookie_failed_accounts:
            error_messages.extend(cookie_failed_accounts)
        if sales_sync_errors:
            error_messages.extend(sales_sync_errors)
        if fetch_errors:
            error_messages.extend(fetch_errors)
        return {
            "items": matched_items,
            "summary": summary,
            "synced": len(cost_data),
            "matched": len(matched_items),
            "missed": missed,
            "error": "；".join(error_messages),
        }
    except Exception as e:
        logger.exception("ERP 同步失败")
        _log(f"[ERP_SYNC:{run_id}] 同步失败: {e}", on_progress)
        _emit_progress_event(
            on_progress_event,
            stage="done",
            current=1,
            total=1,
            message=f"同步失败: {e}",
        )
        return {
            "items": [],
            "summary": str(e),
            "synced": 0,
            "matched": 0,
            "missed": 0,
            "error": str(e),
        }


def task_refresh_imported_items_status(
    account_store: AccountStore,
    imported_store: BatchItemStore,
    on_progress: Optional[Callable[[str], None]] = None,
    *,
    limit: int = 80,
) -> dict:
    """刷新导入商品的实时状态（价格/状态/上架时间等）。"""
    accounts = {account.name: account for account in account_store.enabled_accounts()}
    items = list(imported_store.get_all())
    if not items:
        summary = "导入商品为空，跳过实时刷新"
        _log(summary, on_progress)
        return {
            "total": 0,
            "all_total": 0,
            "eligible_total": 0,
            "processed_total": 0,
            "limit": max(int(limit or 0), 0),
            "truncated": False,
            "ok": 0,
            "skip": 0,
            "fail": 0,
            "changed": 0,
            "unchanged": 0,
            "status_changed": 0,
            "price_changed": 0,
            "settle_changed": 0,
            "listed_time_changed": 0,
            "title_changed": 0,
            "changed_samples": [],
            "status_change_records": [],
            "failed_samples": [],
            "skipped_samples": [],
            "summary": summary,
        }

    safe_limit = max(int(limit or 0), 0)
    all_total = len(items)
    eligible_items = [item for item in items if not getattr(item, "ignored", False)]
    eligible_total = len(eligible_items)
    candidates = eligible_items
    if safe_limit > 0:
        candidates = candidates[:safe_limit]
    processed_total = len(candidates)
    truncated = safe_limit > 0 and eligible_total > safe_limit

    services: dict[str, ImeiService] = {}
    login_state_cache: dict[str, tuple[bool, str]] = {}
    ok = skip = fail = 0
    changed = unchanged = 0
    status_changed = price_changed = settle_changed = listed_time_changed = title_changed = 0
    changed_samples: list[str] = []
    status_change_records: list[dict[str, str]] = []
    failed_samples: list[str] = []
    skipped_samples: list[str] = []

    def _append_sample(target: list[str], text: str, *, max_size: int = 10) -> None:
        if text and len(target) < max_size:
            target.append(text)

    def _fmt_price(value) -> str:
        try:
            return f"{float(value or 0):.0f}"
        except Exception:
            return "0"

    for item in candidates:
        account = accounts.get(item.account_name)
        item_mark = item.qc_code or item.product_id or "-"
        if account is None:
            skip += 1
            _append_sample(skipped_samples, f"[{item_mark}] 跳过：账号不存在或未启用")
            continue

        svc = services.get(account.name)
        if svc is None:
            svc = ImeiService(account.name, account.cookie)
            services[account.name] = svc

        login_ok, login_reason = _account_login_state(svc, account.name, login_state_cache)
        if not login_ok:
            fail += 1
            reason_text = str(login_reason or "登录状态过期，请重新登录").strip()
            _append_sample(failed_samples, f"[{account.name}] [{item_mark}] 失败：账号登录失效（{reason_text}）")
            continue

        try:
            detail = _refresh_imported_detail(svc, item)
        except Exception as exc:
            fail += 1
            _append_sample(failed_samples, f"[{account.name}] [{item_mark}] 失败：刷新异常（{exc}）")
            continue

        if detail is None:
            fail += 1
            _append_sample(failed_samples, f"[{account.name}] [{item_mark}] 失败：未找到商品")
            continue

        old_status = getattr(item, "status", None)
        old_current_price = float(getattr(item, "current_price", 0.0) or 0.0)
        old_settle_price = getattr(item, "settle_price", None)
        old_listed_time = getattr(item, "listed_time", None)
        old_title = str(getattr(item, "title", "") or "")

        source = str(getattr(item, "import_source", "") or "").strip().lower()
        listing_eligible = source in _allowed_listing_import_sources() and detail.status == ProductStatus.NOT_LISTED
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
            imei=getattr(detail, "imei", "") or getattr(item, "imei", ""),
            listing_eligible=listing_eligible,
        )

        status_delta = old_status != detail.status
        price_delta = float(getattr(detail, "current_price", 0.0) or 0.0) != old_current_price
        settle_delta = getattr(detail, "settle_price", None) != old_settle_price
        listed_time_delta = getattr(detail, "listed_time", None) != old_listed_time
        title_delta = str(getattr(detail, "title", "") or "") != old_title

        if status_delta:
            status_changed += 1
            status_change_records.append(
                {
                    "product_id": str(item.product_id or ""),
                    "account_name": str(item.account_name or ""),
                    "qc_code": str(detail.qc_code or item_mark),
                    "title": str(getattr(detail, "title", "") or getattr(item, "title", "") or ""),
                    "old_status": _status_label(old_status),
                    "new_status": _status_label(detail.status),
                    "status_detail": _status_detail_from_obj(detail),
                }
            )
        if price_delta:
            price_changed += 1
        if settle_delta:
            settle_changed += 1
        if listed_time_delta:
            listed_time_changed += 1
        if title_delta:
            title_changed += 1

        if status_delta or price_delta or settle_delta or listed_time_delta or title_delta:
            changed += 1
            changes: list[str] = []
            if status_delta:
                changes.append(f"状态 {_status_label(old_status)}→{_status_label(detail.status)}")
            if price_delta:
                changes.append(f"当前价 {_fmt_price(old_current_price)}→{_fmt_price(getattr(detail, 'current_price', 0.0))}")
            if settle_delta:
                changes.append(f"到手价 {_fmt_price(old_settle_price)}→{_fmt_price(getattr(detail, 'settle_price', 0.0))}")
            if listed_time_delta:
                old_text = old_listed_time.strftime("%m-%d %H:%M") if isinstance(old_listed_time, datetime.datetime) else "-"
                new_listed = getattr(detail, "listed_time", None)
                new_text = new_listed.strftime("%m-%d %H:%M") if isinstance(new_listed, datetime.datetime) else "-"
                changes.append(f"上架时间 {old_text}→{new_text}")
            if title_delta:
                changes.append("标题已更新")
            _append_sample(changed_samples, f"[{account.name}] [{detail.qc_code or item_mark}] {'；'.join(changes)}")
        else:
            unchanged += 1

        ok += 1

    total = len(candidates)
    summary = (
        f"实时刷新完成：处理 {processed_total} 件（总数 {all_total}，可刷新 {eligible_total}），成功 {ok}（变更 {changed}，无变化 {unchanged}），"
        f"跳过 {skip}，失败 {fail}"
    )
    _log(summary, on_progress)
    return {
        "total": total,
        "all_total": all_total,
        "eligible_total": eligible_total,
        "processed_total": processed_total,
        "limit": safe_limit,
        "truncated": truncated,
        "ok": ok,
        "skip": skip,
        "fail": fail,
        "changed": changed,
        "unchanged": unchanged,
        "status_changed": status_changed,
        "price_changed": price_changed,
        "settle_changed": settle_changed,
        "listed_time_changed": listed_time_changed,
        "title_changed": title_changed,
        "changed_samples": changed_samples,
        "status_change_records": status_change_records,
        "failed_samples": failed_samples,
        "skipped_samples": skipped_samples,
        "summary": summary,
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
    skip_bucket: dict[str, int] = {}
    manual_review_count = 0
    persisted_count = 0
    persisted_samples: list[dict[str, Any]] = []
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

        if _should_skip_status_for_auto_reprice(detail.status):
            imported_store.update_item(
                item.product_id,
                reprice_ok=None,
                reprice_msg=f"当前状态 {detail.status.label}",
                op_status="跳过",
                op_message="非在售，自动调价跳过",
            )
            _summarize_skip_bucket(skip_bucket, f"状态:{detail.status.label}")
            skip += 1
            continue

        pipeline = run_reprice_pipeline(
            work_item,
            sold_cache,
            rule_engine,
            apply_rules=True,
            settle_price_estimator=lambda price, product_id=detail.product_id: _estimate_suggested_settle_price(svc, product_id, price),
            official_reference_fetcher=lambda _item, _svc=svc, _detail=detail: _fetch_official_reference_for_item(_svc, _detail),
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
                **_manual_review_fields(
                    decision,
                    source="auto_reprice",
                    target_action="change_price",
                    target_price=new_price,
                ),
            )
            manual_review_count += 1
            _log(
                f"[{account.name}] [{detail.qc_code or item.qc_code}] 待确认 current={detail.current_price:.0f} target={float(new_price or 0):.0f} reason={manual_reason}",
                on_progress,
            )
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
            persisted_count += 1
            _append_persisted_sample(
                persisted_samples,
                task="auto_reprice",
                account=account.name,
                item=getattr(record_source, "qc_code", detail.qc_code) or item.qc_code or item.product_id,
                old_price=float(detail.current_price or 0.0),
                new_price=float(new_price or 0.0),
                trigger="AUTO_REPRICE",
            )
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
            _log(_format_short_reprice_log(item, float(detail.current_price or 0), float(new_price or 0), preview), on_progress)
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

    if skip_bucket:
        bucket_text = "，".join([f"{k}={v}" for k, v in sorted(skip_bucket.items(), key=lambda kv: (-kv[1], kv[0]))[:6]])
        _log(f"auto_reprice 跳过汇总: {bucket_text}", on_progress)
    _log(f"auto_reprice 结果: total={total}, ok={ok}, skip={skip}, fail={fail}, 待确认={manual_review_count}, 已写入={persisted_count}", on_progress)
    return {
        "total": total,
        "ok": ok,
        "skip": skip,
        "fail": fail,
        "manual_review": manual_review_count,
        "skip_bucket": skip_bucket,
        "persisted_count": persisted_count,
        "persisted_samples": persisted_samples,
    }


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
    persisted_count = 0
    persisted_samples: list[dict[str, Any]] = []

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
            official_reference_fetcher=lambda _item, _svc=svc, _detail=detail: _fetch_official_reference_for_item(_svc, _detail),
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
                **_manual_review_fields(
                    decision,
                    source="stale_drop",
                    target_action="change_price",
                    target_price=new_price,
                ),
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
            persisted_count += 1
            _append_persisted_sample(
                persisted_samples,
                task="stale_drop",
                account=account.name,
                item=getattr(record_source, "qc_code", detail.qc_code) or item.qc_code or item.product_id,
                old_price=float(detail.current_price or 0.0),
                new_price=float(new_price or 0.0),
                trigger="AUTO_STALE",
            )
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

    return {
        "total": total,
        "ok": ok,
        "skip": skip,
        "fail": fail,
        "persisted_count": persisted_count,
        "persisted_samples": persisted_samples,
    }


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
    persisted_count = 0
    persisted_samples: list[dict[str, Any]] = []

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
            official_reference_fetcher=lambda _item, _svc=svc, _detail=detail: _fetch_official_reference_for_item(_svc, _detail),
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
                **_manual_review_fields(
                    decision,
                    source="auto_list",
                    target_action="list_product",
                    target_price=price,
                ),
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
            persisted_count += 1
            _append_persisted_sample(
                persisted_samples,
                task="auto_list",
                account=account.name,
                item=getattr(record_source, "qc_code", detail.qc_code) or item.qc_code or item.product_id,
                old_price=0.0,
                new_price=float(price or 0.0),
                trigger="AUTO_LIST",
            )
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

    return {
        "ok": ok,
        "fail": fail,
        "skip": skip,
        "persisted_count": persisted_count,
        "persisted_samples": persisted_samples,
    }



# ─── 任务4：销售播报 ─────────────────────────────────────────

def task_sales_report(
    account_store: AccountStore,
    notifier,
    on_progress: Optional[Callable[[str], None]] = None,
    *,
    imported_store: Optional[BatchItemStore] = None,
    wx_app_client=None,
    limit: int = 80,
) -> str:
    """
    生成并发送销售播报。

    当传入 imported_store 时，仅播报导入商品状态变更。
    """
    if imported_store is None:
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

    refresh_result = task_refresh_imported_items_status(
        account_store,
        imported_store,
        on_progress=on_progress,
        limit=limit,
    )
    status_records = list(refresh_result.get("status_change_records") or [])
    if not status_records:
        report = "导入商品状态无变化，跳过企业微信播报"
        _log(report, on_progress)
        return report

    now = datetime.datetime.now()
    dedupe_state = _load_status_notify_dedupe_state(now)
    pending_records: list[dict] = []
    pending_keys: set[str] = set()
    for record in status_records:
        dedupe_key = _status_notify_dedupe_key(
            str(record.get("product_id") or ""),
            str(record.get("old_status") or ""),
            str(record.get("new_status") or ""),
        )
        if not dedupe_key or dedupe_key in dedupe_state or dedupe_key in pending_keys:
            continue
        pending_records.append(record)
        pending_keys.add(dedupe_key)

    if not pending_records:
        report = "导入商品状态变更均已播报，跳过重复推送"
        _log(report, on_progress)
        return report

    message = _build_status_notify_report(pending_records)
    send_ok = _send_sales_status_report(notifier, wx_app_client, message)
    if send_ok:
        for dedupe_key in pending_keys:
            dedupe_state[dedupe_key] = now
        _save_status_notify_dedupe_state(dedupe_state)
        summary = f"导入商品状态播报发送成功：{len(pending_records)} 条"
    else:
        summary = "导入商品状态播报发送失败（企业微信渠道不可用或发送异常）"

    _log(summary, on_progress)
    return message if send_ok else summary
