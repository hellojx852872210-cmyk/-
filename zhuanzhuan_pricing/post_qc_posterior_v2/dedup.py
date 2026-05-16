from __future__ import annotations

from collections import OrderedDict


def build_dedup_key(row: dict) -> str:
    return "|".join([
        str(row.get("imei") or "").strip(),
        str(row.get("product_id") or "").strip(),
        str(row.get("qc_item_id") or "").strip() or str(row.get("qc_item_name") or "").strip(),
        str(row.get("ori_qc_result") or "").strip(),
        str(row.get("post_qc_result") or "").strip(),
    ])


def deduplicate_rows(rows: list[dict]) -> list[dict]:
    ordered: "OrderedDict[str, dict]" = OrderedDict()
    dup_count: dict[str, int] = {}
    for row in rows:
        key = build_dedup_key(row)
        if not key.strip("|"):
            continue
        if key not in ordered:
            payload = dict(row)
            payload["dedup_key"] = key
            payload["dup_count"] = 1
            ordered[key] = payload
            dup_count[key] = 1
            continue
        dup_count[key] += 1
        ordered[key]["dup_count"] = dup_count[key]
    return list(ordered.values())
