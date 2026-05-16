# -*- coding: utf-8 -*-
"""
改价历史记录 — SQLite 持久化
每次成功改价后写入，供数据看板查询
"""
from __future__ import annotations
import sqlite3
import datetime
import threading
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from ..config import LEGACY_PATHS, PRICE_HISTORY_DB, cfg, ensure_parent_dir
from ..services.mysql_store import get_mysql_store
from .models import PriceChangeRecord, PriceTrigger


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS price_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    qc_code      TEXT,
    title        TEXT,
    model        TEXT,
    condition    TEXT,
    capacity     TEXT,
    color        TEXT,
    old_price    REAL,
    new_price    REAL,
    diff         REAL,
    settle_price REAL,
    trigger      TEXT,
    account_name TEXT,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON price_changes(timestamp);
CREATE INDEX IF NOT EXISTS idx_model     ON price_changes(model);
CREATE INDEX IF NOT EXISTS idx_account   ON price_changes(account_name);
"""


class PriceHistoryDB:
    """线程安全的改价历史数据库"""

    _instance: Optional["PriceHistoryDB"] = None

    @classmethod
    def get(cls, db_path: str = PRICE_HISTORY_DB) -> "PriceHistoryDB":
        """获取全局单例"""
        if cls._instance is None:
            cls._instance = cls(db_path)
        return cls._instance

    def __init__(self, db_path: str = PRICE_HISTORY_DB):
        requested_path = Path(db_path)
        self._legacy_path = Path(LEGACY_PATHS["price_history"]) if db_path == PRICE_HISTORY_DB else None
        self._current_path = ensure_parent_dir(requested_path)
        self.db_path = str(self._resolve_db_path())
        self._lock = threading.Lock()
        self._init_db()

    def _resolve_db_path(self) -> Path:
        current = self._current_path
        legacy = self._legacy_path
        if current.exists():
            return current
        if legacy and legacy.exists():
            return legacy
        return current

    def _migrate_legacy_db_if_needed(self) -> None:
        if not self._legacy_path or not self._legacy_path.exists():
            return
        target = self._current_path
        source = Path(self.db_path)
        if target.exists() or source.resolve() == target.resolve():
            self.db_path = str(target)
            return
        ensure_parent_dir(target)
        try:
            shutil.copy2(source, target)
            self.db_path = str(target)
        except Exception:
            self.db_path = str(source)

    @contextmanager
    def _connect(self):
        self._migrate_legacy_db_if_needed()
        ensure_parent_dir(self._current_path)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript(CREATE_SQL)

    # ── 写入 ──────────────────────────────────────────────────

    def record(self, r: PriceChangeRecord) -> int:
        """写入一条改价记录，返回主键 id"""
        sql = """
        INSERT INTO price_changes
            (timestamp, product_id, qc_code, title, model, condition, capacity, color,
             old_price, new_price, diff, settle_price, trigger, account_name, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        params = (
            r.timestamp.isoformat(),
            r.product_id, r.qc_code, r.title,
            r.model, r.condition, r.capacity, r.color,
            r.old_price, r.new_price, r.diff, r.settle_price,
            r.trigger.value if isinstance(r.trigger, PriceTrigger) else r.trigger,
            r.account_name, r.note,
        )
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, params)
            row_id = cur.lastrowid
        try:
            get_mysql_store().write_price_changes([{
                "id": row_id,
                "timestamp": r.timestamp.isoformat(),
                "product_id": r.product_id,
                "qc_code": r.qc_code,
                "title": r.title,
                "model": r.model,
                "condition": r.condition,
                "capacity": r.capacity,
                "color": r.color,
                "old_price": r.old_price,
                "new_price": r.new_price,
                "diff": r.diff,
                "settle_price": r.settle_price,
                "trigger": (r.trigger.value if isinstance(r.trigger, PriceTrigger) else r.trigger),
                "account_name": r.account_name,
                "note": r.note,
            }])
        except Exception:
            pass
        return row_id

    def record_many(self, records: List[PriceChangeRecord]):
        """批量写入"""
        sql = """
        INSERT INTO price_changes
            (timestamp, product_id, qc_code, title, model, condition, capacity, color,
             old_price, new_price, diff, settle_price, trigger, account_name, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        rows = [(
            r.timestamp.isoformat(),
            r.product_id, r.qc_code, r.title,
            r.model, r.condition, r.capacity, r.color,
            r.old_price, r.new_price, r.diff, r.settle_price,
            r.trigger.value if isinstance(r.trigger, PriceTrigger) else r.trigger,
            r.account_name, r.note,
        ) for r in records]
        with self._lock, self._connect() as conn:
            conn.executemany(sql, rows)
        try:
            get_mysql_store().write_price_changes([
                {
                    "timestamp": r.timestamp.isoformat(),
                    "product_id": r.product_id,
                    "qc_code": r.qc_code,
                    "title": r.title,
                    "model": r.model,
                    "condition": r.condition,
                    "capacity": r.capacity,
                    "color": r.color,
                    "old_price": r.old_price,
                    "new_price": r.new_price,
                    "diff": r.diff,
                    "settle_price": r.settle_price,
                    "trigger": (r.trigger.value if isinstance(r.trigger, PriceTrigger) else r.trigger),
                    "account_name": r.account_name,
                    "note": r.note,
                }
                for r in records
            ])
        except Exception:
            pass

    # ── 查询 ──────────────────────────────────────────────────

    def query(
        self,
        model: str = "",
        account: str = "",
        trigger: str = "",
        days: int = 30,
        limit: int = 500,
    ) -> List[PriceChangeRecord]:
        """
        灵活查询改价记录

        :param model:   型号关键词（模糊）
        :param account: 账号名（精确）
        :param trigger: 触发方式（精确）
        :param days:    最近 N 天
        :param limit:   最多返回条数
        """
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        wheres = ["timestamp >= ?"]
        params: list = [cutoff]

        if model:
            wheres.append("model LIKE ?")
            params.append(f"%{model}%")
        if account:
            wheres.append("account_name = ?")
            params.append(account)
        if trigger:
            wheres.append("trigger = ?")
            params.append(trigger)

        if bool(cfg.get("mysql_primary_read_enabled", False)):
            ok, _msg, mysql_rows = get_mysql_store().load_price_changes(days=days, limit=limit)
            if ok and mysql_rows:
                return [self._mysql_row_to_record(row) for row in mysql_rows]

        where_clause = " AND ".join(wheres)
        sql = (f"SELECT * FROM price_changes WHERE {where_clause} "
               f"ORDER BY timestamp DESC LIMIT {limit}")

        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def daily_summary(self, days: int = 7) -> List[dict]:
        """
        每日改价统计
        返回 [{"date": "2025-01-01", "count": 10, "avg_diff": -50.0}, ...]
        """
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        sql = """
        SELECT substr(timestamp, 1, 10) as date,
               COUNT(*) as count,
               AVG(diff) as avg_diff,
               SUM(CASE WHEN diff < 0 THEN 1 ELSE 0 END) as drops,
               SUM(CASE WHEN diff > 0 THEN 1 ELSE 0 END) as raises
        FROM price_changes
        WHERE timestamp >= ?
        GROUP BY date
        ORDER BY date
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, [cutoff]).fetchall()
        return [dict(r) for r in rows]

    def model_summary(self, days: int = 30, limit: int = 20) -> List[dict]:
        """按型号统计改价次数和平均降幅"""
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        sql = """
        SELECT model, COUNT(*) as count, AVG(diff) as avg_diff
        FROM price_changes
        WHERE timestamp >= ?
        GROUP BY model
        ORDER BY count DESC
        LIMIT ?
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, [cutoff, limit]).fetchall()
        return [dict(r) for r in rows]

    def total_count(self, days: int = 30) -> int:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM price_changes WHERE timestamp >= ?", [cutoff]
            ).fetchone()
        return row["c"] if row else 0

    # ── 内部转换 ──────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row) -> PriceChangeRecord:
        try:
            trigger = PriceTrigger(row["trigger"])
        except (ValueError, KeyError):
            trigger = PriceTrigger.MANUAL
        return PriceChangeRecord(
            id=row["id"],
            timestamp=datetime.datetime.fromisoformat(row["timestamp"]),
            product_id=row["product_id"],
            qc_code=row["qc_code"] or "",
            title=row["title"] or "",
            model=row["model"] or "",
            condition=row["condition"] or "",
            capacity=row["capacity"] or "",
            color=row["color"] or "",
            old_price=row["old_price"] or 0.0,
            new_price=row["new_price"] or 0.0,
            diff=row["diff"] or 0.0,
            settle_price=row["settle_price"] or 0.0,
            trigger=trigger,
            account_name=row["account_name"] or "",
            note=row["note"] or "",
        )

    @staticmethod
    def _mysql_row_to_record(row: dict) -> PriceChangeRecord:
        try:
            trigger = PriceTrigger(str(row.get("trigger") or "manual"))
        except Exception:
            trigger = PriceTrigger.MANUAL
        ts = row.get("timestamp_dt")
        if isinstance(ts, datetime.datetime):
            timestamp = ts
        else:
            try:
                timestamp = datetime.datetime.fromisoformat(str(ts))
            except Exception:
                timestamp = datetime.datetime.now()
        source_id = row.get("source_id")
        try:
            parsed_id = int(source_id) if source_id is not None else 0
        except Exception:
            parsed_id = 0
        return PriceChangeRecord(
            id=parsed_id,
            timestamp=timestamp,
            product_id=str(row.get("product_id") or ""),
            qc_code=str(row.get("qc_code") or ""),
            title=str(row.get("title") or ""),
            model=str(row.get("model") or ""),
            condition=str(row.get("condition") or ""),
            capacity=str(row.get("capacity") or ""),
            color=str(row.get("color") or ""),
            old_price=float(row.get("old_price") or 0.0),
            new_price=float(row.get("new_price") or 0.0),
            diff=float(row.get("diff") or 0.0),
            settle_price=float(row.get("settle_price") or 0.0),
            trigger=trigger,
            account_name=str(row.get("account_name") or ""),
            note=str(row.get("note") or ""),
        )


# 全局单例
_history_db: Optional[PriceHistoryDB] = None

def get_history_db() -> PriceHistoryDB:
    global _history_db
    if _history_db is None:
        _history_db = PriceHistoryDB()
    return _history_db