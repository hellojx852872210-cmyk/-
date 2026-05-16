# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Callable

from ..config import CACHE_FILE, PRICE_HISTORY_DB
from ..services.mysql_store import get_mysql_store


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PROJECT_ROOT / "runtime"


def _load_cycle_logs() -> list[dict]:
    path = RUNTIME_DIR / "agent_cycle_log.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = str(line or "").strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _load_sold_records() -> list[dict]:
    path = Path(CACHE_FILE)
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(dict(row))
    return rows


def _load_price_changes() -> list[dict]:
    path = Path(PRICE_HISTORY_DB)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        data = conn.execute("SELECT * FROM price_changes ORDER BY id ASC").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in data]


def _load_batch_snapshot_items() -> list[dict]:
    path = RUNTIME_DIR / "imported_items_pool.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _load_post_qc_rows() -> list[dict]:
    path = RUNTIME_DIR / "post_qc_intercepts_cache.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _chunks(rows: list[dict], size: int = 500) -> list[list[dict]]:
    return [rows[idx:idx + size] for idx in range(0, len(rows), size)]


def _write_chunk_with_fallback(write_fn, block: list[dict]) -> tuple[int, int, str]:
    ok, msg = write_fn(block)
    if ok:
        return len(block), 0, msg
    success = 0
    failed = 0
    last_msg = msg
    for row in block:
        row_ok, row_msg = write_fn([row])
        last_msg = row_msg
        if row_ok:
            success += 1
        else:
            failed += 1
    return success, failed, last_msg


def _safe_progress(on_progress: Callable[[str, int, int, float, str], None] | None, stage: str, current: int, total: int, message: str) -> None:
    if not callable(on_progress):
        return
    total_val = max(int(total or 0), 0)
    current_val = max(min(int(current or 0), total_val), 0) if total_val > 0 else 0
    pct = 1.0 if total_val <= 0 else (current_val / float(total_val))
    try:
        on_progress(stage, current_val, total_val, pct, message)
    except Exception:
        return


def migrate(on_progress: Callable[[str, int, int, float, str], None] | None = None) -> int:
    store = get_mysql_store()

    cycle_logs = _load_cycle_logs()
    sold_records = _load_sold_records()
    price_changes = _load_price_changes()
    batch_items = _load_batch_snapshot_items()
    post_qc_rows = _load_post_qc_rows()

    _safe_progress(on_progress, "prepare", 0, 1, "开始读取并准备迁移数据")
    _safe_progress(on_progress, "prepare", 1, 1, "迁移数据准备完成")

    last_message = "ok"
    migrated = {"cycle": 0, "sold": 0, "price": 0, "batch": 0, "post_qc": 0}
    failed = {"sold": 0, "price": 0, "post_qc": 0}

    cycle_total = len(cycle_logs)
    _safe_progress(on_progress, "cycle_logs", 0, cycle_total, "开始同步 cycle_logs")
    for payload in cycle_logs:
        ok, msg = store.write_cycle_log(payload)
        last_message = msg
        if ok:
            migrated["cycle"] += 1
        _safe_progress(on_progress, "cycle_logs", migrated["cycle"], cycle_total, "同步 cycle_logs")

    sold_total = len(sold_records)
    _safe_progress(on_progress, "sold_records", 0, sold_total, "开始同步 sold_records")
    sold_done = 0
    for block in _chunks(sold_records, 500):
        ok_count, fail_count, msg = _write_chunk_with_fallback(store.write_sold_records, block)
        last_message = msg
        migrated["sold"] += ok_count
        failed["sold"] += fail_count
        sold_done += len(block)
        _safe_progress(on_progress, "sold_records", sold_done, sold_total, "同步 sold_records")

    price_total = len(price_changes)
    _safe_progress(on_progress, "price_changes", 0, price_total, "开始同步 price_changes")
    price_done = 0
    for block in _chunks(price_changes, 500):
        ok_count, fail_count, msg = _write_chunk_with_fallback(store.write_price_changes, block)
        last_message = msg
        migrated["price"] += ok_count
        failed["price"] += fail_count
        price_done += len(block)
        _safe_progress(on_progress, "price_changes", price_done, price_total, "同步 price_changes")

    batch_total = len(batch_items)
    _safe_progress(on_progress, "batch_snapshot", 0, batch_total, "开始同步 batch_snapshot")
    if batch_items:
        import datetime

        ok, msg = store.write_batch_snapshot(datetime.datetime.now(), batch_items)
        last_message = msg
        if ok:
            migrated["batch"] = len(batch_items)
    _safe_progress(on_progress, "batch_snapshot", migrated["batch"], batch_total, "同步 batch_snapshot")

    post_qc_total = len(post_qc_rows)
    _safe_progress(on_progress, "post_qc_intercepts", 0, post_qc_total, "开始同步 post_qc_intercepts")
    post_qc_done = 0
    for block in _chunks(post_qc_rows, 500):
        ok_count, fail_count, msg = _write_chunk_with_fallback(store.write_post_qc_intercepts, block)
        last_message = msg
        migrated["post_qc"] += ok_count
        failed["post_qc"] += fail_count
        post_qc_done += len(block)
        _safe_progress(on_progress, "post_qc_intercepts", post_qc_done, post_qc_total, "同步 post_qc_intercepts")

    print("Migration summary:")
    print(f"- cycle_logs: {migrated['cycle']}/{len(cycle_logs)}")
    print(f"- sold_records: {migrated['sold']}/{len(sold_records)} (failed={failed['sold']})")
    print(f"- price_changes: {migrated['price']}/{len(price_changes)} (failed={failed['price']})")
    print(f"- batch_snapshot: {migrated['batch']}/{len(batch_items)}")
    print(f"- post_qc_intercepts: {migrated['post_qc']}/{len(post_qc_rows)} (failed={failed['post_qc']})")
    print(f"- last_message: {last_message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(migrate())
