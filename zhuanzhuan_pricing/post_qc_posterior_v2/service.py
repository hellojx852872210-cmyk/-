from __future__ import annotations

from collections import defaultdict

from ..services.data_store import AccountStore
from ..services.zhuanzhuan_api import ImeiService
from .models import InterceptRow


def _is_abnormal_reason_text(text: str) -> bool:
    tokens = [
        "异常",
        "问题",
        "破",
        "裂",
        "坏",
        "松",
        "掉",
        "失灵",
        "漏液",
        "烧",
        "锈",
        "进灰",
        "进水",
        "黑点",
        "白斑",
        "闪",
        "触控",
        "Face ID",
        "电池",
        "维修",
        "更换",
        "划痕",
        "磕碰",
    ]
    low = str(text or "").lower()
    if not low:
        return False
    return any(t.lower() in low for t in tokens)


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _expand_subitems(qc_item_name: str, ori_result: str, post_result: str) -> list[tuple[str, str, str]]:
    def parse_pairs(text: str) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for chunk in str(text or "").split(","):
            part = chunk.strip()
            if not part or ":" not in part:
                continue
            key, val = part.split(":", 1)
            key = key.strip()
            val = val.strip()
            if key:
                pairs[key] = val
        return pairs

    ori_pairs = parse_pairs(ori_result)
    post_pairs = parse_pairs(post_result)
    if not ori_pairs and not post_pairs:
        return [(qc_item_name, _normalize_text(ori_result), _normalize_text(post_result))]

    rows: list[tuple[str, str, str]] = []
    for key in sorted(set(ori_pairs.keys()) | set(post_pairs.keys())):
        ori_val = _normalize_text(ori_pairs.get(key))
        post_val = _normalize_text(post_pairs.get(key))
        if not post_val:
            continue
        if ori_val == post_val:
            continue
        sub_name = f"{qc_item_name}/{key}" if qc_item_name else key
        rows.append((sub_name, ori_val, post_val))
    return rows


def collect_label_pool_for_imeis(
    *,
    imeis: list[str],
    statuses: tuple[str, ...] = ("80", "100", "1"),
    page_size: int = 50,
    max_pages: int = 3,
) -> tuple[list[dict], dict]:
    accounts = [a for a in AccountStore().enabled_accounts() if _normalize_text(getattr(a, "cookie", ""))]
    if not accounts:
        raise RuntimeError("没有可用账号（enabled + cookie）")

    imei_set = {str(x).strip() for x in imeis if str(x).strip()}
    rows: list[dict] = []
    stats: dict[str, int] = defaultdict(int)
    seen_keys: set[str] = set()

    for account in accounts:
        svc = ImeiService(account.name, account.cookie)
        for status_code in statuses:
            for page in range(1, max_pages + 1):
                payload = {
                    "pageNum": page,
                    "pageSize": page_size,
                    "tagIds": [],
                    "noTagIds": [],
                    "labels": ["POST_QC_SUB_INTERCEPT"],
                    "statusList": [str(status_code)],
                    "salesInShop": False,
                }
                try:
                    body = svc._merchant_product_list(payload)
                except Exception:
                    stats["pool_query_fail"] += 1
                    continue

                resp_data = (body.get("respData") or body.get("data") or {}) if isinstance(body, dict) else {}
                items = resp_data.get("list") or []
                stats["pool_pages"] += 1
                if not items:
                    break

                stats["pool_items_total"] += len(items)
                for item in items:
                    imei = _normalize_text(item.get("imei"))
                    if imei not in imei_set:
                        continue
                    product_id = _normalize_text(item.get("productId"))
                    key = "|".join([_normalize_text(account.name), str(status_code), product_id, imei])
                    if key in seen_keys:
                        stats["pool_hit_duplicate"] += 1
                        continue
                    seen_keys.add(key)
                    state = item.get("state") or {}
                    lifecycle = item.get("lifecycleTimes") or {}
                    apply_return_time = _normalize_text(lifecycle.get("applyReturnTime"))
                    sold_time = _normalize_text(lifecycle.get("soldTime"))
                    event_time = apply_return_time or sold_time
                    rows.append({
                        "account_name": _normalize_text(account.name),
                        "source_status_query": str(status_code),
                        "product_id": product_id,
                        "qc_code": _normalize_text(item.get("qcCode")),
                        "imei": imei,
                        "title": _normalize_text(item.get("title")),
                        "status_code": _normalize_text(state.get("status")),
                        "status_name": _normalize_text(state.get("statusName")),
                        "label_list": "|".join([str(x) for x in (item.get("labelList") or [])]),
                        "sold_time": sold_time,
                        "apply_return_time": apply_return_time,
                        "event_time": event_time,
                    })
                    stats["pool_hit_rows"] += 1

                if len(items) < page_size:
                    break

    status_counts: dict[str, int] = defaultdict(int)
    imei_hits: dict[str, int] = defaultdict(int)
    for row in rows:
        status_counts[str(row.get("status_name") or "")] += 1
        imei_hits[str(row.get("imei") or "")] += 1

    imei_distribution = {"zero": 0, "one": 0, "multi": 0}
    for imei in imei_set:
        hit = int(imei_hits.get(imei, 0))
        if hit == 0:
            imei_distribution["zero"] += 1
        elif hit == 1:
            imei_distribution["one"] += 1
        else:
            imei_distribution["multi"] += 1

    result_stats = {
        "accounts": [str(a.name) for a in accounts],
        "counts": dict(stats),
        "status_counts": dict(status_counts),
        "imei_distribution": imei_distribution,
        "imei_total": len(imei_set),
    }
    return rows, result_stats


def check_single_item_intercept(
    *,
    record: dict,
    account_cookie_map: dict[str, str],
) -> tuple[list[dict], str]:
    account_name = _normalize_text(record.get("account_name"))
    qc_code = _normalize_text(record.get("qc_code"))
    product_id = _normalize_text(record.get("product_id"))
    imei = _normalize_text(record.get("imei"))

    cookie = _normalize_text(account_cookie_map.get(account_name))
    if not account_name or not cookie:
        return [], "account_cookie_missing"

    svc = ImeiService(account_name, cookie)

    detail = None
    try:
        if qc_code:
            detail = svc.query_by_qc_code(qc_code)
        if detail is None and imei:
            detail = svc.query_by_imei(imei)
        if detail is None and product_id:
            detail = svc._query_product_by_id(product_id, statuses=("0", "60", "80", "1"))
    except Exception:
        detail = None

    status_label = _normalize_text(getattr(getattr(detail, "status", None), "label", ""))
    resolved_qc_code = _normalize_text(getattr(detail, "qc_code", "")) or qc_code
    resolved_product_id = _normalize_text(getattr(detail, "product_id", "")) or product_id
    resolved_imei = _normalize_text(getattr(detail, "imei", "")) or imei

    if not resolved_qc_code and not resolved_product_id:
        return [], "missing_qc_and_product"

    try:
        diff_items = svc.query_post_qc_diff(qc_code=resolved_qc_code, product_id=resolved_product_id)
    except Exception:
        diff_items = []

    rows: list[dict] = []
    for diff in diff_items or []:
        qc_item_id = _normalize_text(diff.get("qcItemId"))
        qc_item_name = _normalize_text(diff.get("qcItemName"))
        ori_result = _normalize_text(diff.get("oriQcResult"))
        post_result = _normalize_text(diff.get("postQcResult"))
        if not post_result:
            continue
        for sub_name, sub_ori, sub_post in _expand_subitems(qc_item_name, ori_result, post_result):
            if not sub_post or sub_ori == sub_post:
                continue
            if not _is_abnormal_reason_text(f"{sub_name}:{sub_post}"):
                continue
            row = InterceptRow(
                imei=resolved_imei,
                account_name=account_name,
                product_id=resolved_product_id,
                qc_code=resolved_qc_code,
                title=_normalize_text(getattr(detail, "title", "")) or _normalize_text(record.get("title")),
                model=_normalize_text(getattr(detail, "model", "")) or _normalize_text(record.get("model")),
                status=status_label or _normalize_text(record.get("new_status")),
                qc_item_id=qc_item_id,
                qc_item_name=sub_name,
                ori_qc_result=sub_ori,
                post_qc_result=sub_post,
                flawed_photos_count=len(diff.get("flawedPhotos") or []),
            ).to_dict()
            row["old_status"] = _normalize_text(record.get("old_status"))
            row["new_status"] = _normalize_text(record.get("new_status"))
            row["status_detail"] = _normalize_text(record.get("status_detail"))
            row["event_time"] = _normalize_text(record.get("event_time"))
            row["source_channel"] = "status_refresh_single"
            row["event_type"] = "post_qc_intercept"
            row["is_intercept"] = 1
            row["intercept_reason"] = sub_post
            rows.append(row)

    if rows:
        return rows, f"diff_hit:{len(rows)}"

    fallback = dict(record)
    fallback["source_channel"] = "status_refresh_single"
    fallback["event_type"] = "status_regression"
    fallback["is_intercept"] = 1
    fallback["intercept_reason"] = "detail_missing_manual_review"
    fallback["qc_code"] = resolved_qc_code or qc_code
    fallback["product_id"] = resolved_product_id or product_id
    fallback["imei"] = resolved_imei or imei
    return [fallback], "detail_missing_manual_review"


def collect_from_imeis(imeis: list[str]) -> tuple[list[dict], dict]:
    accounts = [a for a in AccountStore().enabled_accounts() if _normalize_text(getattr(a, "cookie", ""))]
    if not accounts:
        raise RuntimeError("没有可用账号（enabled + cookie）")

    rows: list[dict] = []
    stats: dict[str, int] = defaultdict(int)
    per_imei_hits: dict[str, int] = defaultdict(int)

    for account in accounts:
        svc = ImeiService(account.name, account.cookie)
        for imei in imeis:
            stats["scanned"] += 1
            try:
                detail = svc.query_by_imei(imei)
            except Exception:
                stats["query_fail"] += 1
                continue
            if detail is None:
                stats["not_found"] += 1
                continue

            status_label = _normalize_text(getattr(getattr(detail, "status", None), "label", ""))

            try:
                diff_items = svc.query_post_qc_diff(
                    qc_code=_normalize_text(getattr(detail, "qc_code", "")),
                    product_id=_normalize_text(getattr(detail, "product_id", "")),
                )
            except Exception:
                stats["diff_fail"] += 1
                continue

            if not diff_items:
                stats["no_diff"] += 1
                continue

            for diff in diff_items:
                qc_item_id = _normalize_text(diff.get("qcItemId"))
                qc_item_name = _normalize_text(diff.get("qcItemName"))
                ori_result = _normalize_text(diff.get("oriQcResult"))
                post_result = _normalize_text(diff.get("postQcResult"))
                if not post_result:
                    continue
                for sub_name, sub_ori, sub_post in _expand_subitems(qc_item_name, ori_result, post_result):
                    if not sub_post:
                        continue
                    if sub_ori == sub_post:
                        continue
                    row = InterceptRow(
                        imei=imei,
                        account_name=_normalize_text(account.name),
                        product_id=_normalize_text(getattr(detail, "product_id", "")),
                        qc_code=_normalize_text(getattr(detail, "qc_code", "")),
                        title=_normalize_text(getattr(detail, "title", "")),
                        model=_normalize_text(getattr(detail, "model", "")),
                        status=status_label,
                        qc_item_id=qc_item_id,
                        qc_item_name=sub_name,
                        ori_qc_result=sub_ori,
                        post_qc_result=sub_post,
                        flawed_photos_count=len(diff.get("flawedPhotos") or []),
                    ).to_dict()
                    row["is_intercept"] = 0
                    row["intercept_reason"] = "baseline_no_rule"
                    rows.append(row)
                    per_imei_hits[imei] += 1
                    stats["diff_rows"] += 1

    imei_distribution = {"zero": 0, "one": 0, "multi": 0}
    for imei in imeis:
        hit = int(per_imei_hits.get(imei, 0))
        if hit == 0:
            imei_distribution["zero"] += 1
        elif hit == 1:
            imei_distribution["one"] += 1
        else:
            imei_distribution["multi"] += 1

    result_stats = {
        "accounts": [str(a.name) for a in accounts],
        "counts": dict(stats),
        "imei_distribution": imei_distribution,
        "imei_total": len(imeis),
    }
    return rows, result_stats
