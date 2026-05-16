# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from typing import Iterable

import pymysql

from ..config import cfg


class MySQLStore:
    _instance = None
    _lock = threading.Lock()
    _COUNTABLE_TABLES = {
        "agent_cycle_logs",
        "price_changes",
        "sold_records",
        "batch_items_snapshot",
        "post_qc_intercepts",
        "agent_usage_logs",
    }

    @classmethod
    def get(cls) -> "MySQLStore":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        default_host = str(cfg.get("mysql_center_host", "127.0.0.1") or "127.0.0.1")
        default_port = int(cfg.get("mysql_center_port", 3306) or 3306)
        default_user = str(cfg.get("mysql_center_user", "zz_agent") or "zz_agent")
        default_password = str(cfg.get("mysql_center_password", "") or "")
        default_database = str(cfg.get("mysql_center_database", "zhuanzhuan_agent") or "zhuanzhuan_agent")
        default_socket = str(cfg.get("mysql_center_socket", "") or "")

        self.host = str(os.environ.get("ZZ_MYSQL_HOST") or cfg.get("mysql_host", default_host) or default_host)
        self.port = int(os.environ.get("ZZ_MYSQL_PORT") or cfg.get("mysql_port", default_port) or default_port)
        self.user = str(os.environ.get("ZZ_MYSQL_USER") or cfg.get("mysql_user", default_user) or default_user)
        self.password = str(os.environ.get("ZZ_MYSQL_PASSWORD") or cfg.get("mysql_password", default_password) or default_password)
        self.database = str(os.environ.get("ZZ_MYSQL_DATABASE") or cfg.get("mysql_database", default_database) or default_database)
        self.unix_socket = str(os.environ.get("ZZ_MYSQL_SOCKET") or cfg.get("mysql_socket", default_socket) or "")
        self.connect_timeout = int(os.environ.get("ZZ_MYSQL_CONNECT_TIMEOUT") or cfg.get("mysql_connect_timeout", 3) or 3)
        self.enabled = bool(cfg.get("mysql_dual_write_enabled", True))
        self._schema_ensured = False

    def _connect(self):
        kwargs = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.connect_timeout,
            "write_timeout": self.connect_timeout,
        }
        if self.unix_socket:
            kwargs["unix_socket"] = self.unix_socket
        return pymysql.connect(**kwargs)

    def _safe_exec_many(self, sql: str, rows: list[tuple]) -> tuple[bool, str]:
        if not self.enabled:
            return False, "mysql dual write disabled"
        if not rows:
            return True, "empty"
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
            finally:
                conn.close()
            return True, "ok"
        except Exception as exc:
            return False, str(exc)

    def _safe_query(self, sql: str, params: tuple | list | None = None) -> tuple[bool, str, list[dict]]:
        if not self.enabled:
            return False, "mysql dual write disabled", []
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params or ())
                    rows = list(cur.fetchall() or [])
            finally:
                conn.close()
            return True, "ok", rows
        except Exception as exc:
            return False, str(exc), []

    @staticmethod
    def _hash_obj(obj) -> str:
        return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_dt(text: str):
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            return dt.replace(microsecond=0)
        except Exception:
            return None

    @staticmethod
    def _is_missing_index_error(message: str) -> bool:
        text = str(message or "").lower()
        return "on duplicate key" in text and ("duplicate" in text or "key" in text)

    def _ensure_sold_records_unique_key(self) -> tuple[bool, str]:
        if self._schema_ensured:
            return True, "already ensured"
        if not self.enabled:
            return False, "mysql dual write disabled"
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM information_schema.statistics "
                        "WHERE table_schema=%s AND table_name='sold_records' AND index_name='uk_product_sold_time'",
                        (self.database,),
                    )
                    row = cur.fetchone() or {}
                    if int(row.get("c") or 0) <= 0:
                        cur.execute(
                            "ALTER TABLE sold_records "
                            "ADD UNIQUE KEY uk_product_sold_time (product_id, sold_time)"
                        )
                self._schema_ensured = True
                return True, "ok"
            finally:
                conn.close()
        except Exception as exc:
            return False, str(exc)

    def write_cycle_log(self, payload: dict) -> tuple[bool, str]:
        row_hash = self._hash_obj(payload)
        source_ts = self._parse_dt(payload.get("ts"))
        row = (
            source_ts,
            int(payload.get("cycle") or 0) or None,
            str(payload.get("run_mode") or "run"),
            str(payload.get("agent_user_id") or "anonymous"),
            json.dumps(payload.get("tasks") or [], ensure_ascii=False),
            json.dumps(payload.get("task_rows") or [], ensure_ascii=False),
            json.dumps(payload.get("persisted_rows") or [], ensure_ascii=False),
            json.dumps(payload.get("risk_stats") or {}, ensure_ascii=False),
            str(payload.get("risk_summary") or ""),
            json.dumps(payload.get("inventory_change_rows") or [], ensure_ascii=False),
            json.dumps(payload.get("turnover_metrics") or {}, ensure_ascii=False),
            str(payload.get("post_manual_review_mode") or ""),
            json.dumps(payload.get("post_manual_review_source_filter") or [], ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False, default=str),
            row_hash,
        )
        sql = (
            "INSERT INTO agent_cycle_logs "
            "(source_ts,cycle,run_mode,agent_user_id,tasks_json,task_rows_json,persisted_rows_json,risk_stats_json,risk_summary,inventory_change_rows_json,turnover_metrics_json,post_manual_review_mode,post_manual_review_source_filter_json,raw_payload_json,payload_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE ingested_at=CURRENT_TIMESTAMP"
        )
        return self._safe_exec_many(sql, [row])

    def write_usage_log(self, entry: dict) -> tuple[bool, str]:
        row_hash = self._hash_obj(entry)
        source_ts = self._parse_dt(entry.get("ts"))
        row = (
            source_ts,
            str(entry.get("agent_user_id") or "anonymous"),
            str(entry.get("command") or "run"),
            str(entry.get("tasks") or ""),
            int(entry.get("ok") or 0),
            int(entry.get("skip") or 0),
            int(entry.get("fail") or 0),
            int(entry.get("manual_review") or 0),
            int(entry.get("persisted") or 0),
            str(entry.get("turnover_rate") or ""),
            str(entry.get("note") or ""),
            json.dumps(entry, ensure_ascii=False, default=str),
            row_hash,
        )
        sql = (
            "INSERT INTO agent_usage_logs "
            "(source_ts,agent_user_id,command_name,tasks,ok_count,skip_count,fail_count,manual_review_count,persisted_count,turnover_rate,note,raw_entry_json,entry_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE ingested_at=CURRENT_TIMESTAMP"
        )
        return self._safe_exec_many(sql, [row])

    def write_price_changes(self, records: Iterable[dict]) -> tuple[bool, str]:
        rows = []
        for rec in records:
            row_hash = self._hash_obj(rec)
            rows.append((
                rec.get("id"),
                self._parse_dt(rec.get("timestamp")),
                str(rec.get("product_id") or ""),
                str(rec.get("qc_code") or ""),
                str(rec.get("title") or ""),
                str(rec.get("model") or ""),
                str(rec.get("condition") or ""),
                str(rec.get("capacity") or ""),
                str(rec.get("color") or ""),
                rec.get("old_price"),
                rec.get("new_price"),
                rec.get("diff"),
                rec.get("settle_price"),
                str(rec.get("trigger") or ""),
                str(rec.get("account_name") or ""),
                str(rec.get("note") or ""),
                row_hash,
            ))
        sql = (
            "INSERT INTO price_changes "
            "(source_id,timestamp_dt,product_id,qc_code,title,model,`condition`,capacity,color,old_price,new_price,diff,settle_price,`trigger`,account_name,note,record_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE ingested_at=CURRENT_TIMESTAMP"
        )
        return self._safe_exec_many(sql, rows)

    def write_sold_records(self, records: Iterable[dict]) -> tuple[bool, str]:
        ok, reason = self._ensure_sold_records_unique_key()
        if not ok:
            return False, f"ensure sold_records unique key failed: {reason}"

        rows = []
        for rec in records:
            sold_dt = self._parse_dt(rec.get("sold_time"))
            if sold_dt is None:
                continue
            row_hash = self._hash_obj(rec)
            rows.append((
                str(rec.get("product_id") or ""),
                str(rec.get("title") or ""),
                rec.get("sold_price"),
                sold_dt,
                rec.get("hours_to_sell"),
                str(rec.get("source") or ""),
                str(rec.get("model") or ""),
                str(rec.get("condition") or ""),
                str(rec.get("capacity") or ""),
                str(rec.get("color") or ""),
                self._parse_dt(rec.get("list_time")),
                (None if rec.get("settle_price") in (None, "") else rec.get("settle_price")),
                row_hash,
            ))
        sql = (
            "INSERT INTO sold_records "
            "(product_id,title,sold_price,sold_time,hours_to_sell,source,model,`condition`,capacity,color,list_time,settle_price,record_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE "
            "title=VALUES(title),sold_price=VALUES(sold_price),hours_to_sell=VALUES(hours_to_sell),"
            "source=VALUES(source),model=VALUES(model),`condition`=VALUES(`condition`),capacity=VALUES(capacity),"
            "color=VALUES(color),list_time=VALUES(list_time),settle_price=VALUES(settle_price),"
            "record_hash=VALUES(record_hash),ingested_at=CURRENT_TIMESTAMP"
        )
        ok, msg = self._safe_exec_many(sql, rows)
        if ok:
            return ok, msg
        if self._is_missing_index_error(msg):
            ensure_ok, ensure_msg = self._ensure_sold_records_unique_key()
            if not ensure_ok:
                return False, f"write failed and ensure unique key retry failed: {ensure_msg}"
            return self._safe_exec_many(sql, rows)
        return ok, msg

    def write_batch_snapshot(self, snapshot_ts: datetime.datetime, items: Iterable[dict]) -> tuple[bool, str]:
        rows = []
        for item in items:
            row_hash = self._hash_obj({"snapshot_ts": snapshot_ts.isoformat(), "item": item})
            rows.append((
                snapshot_ts,
                str(item.get("product_id") or ""),
                str(item.get("qc_code") or ""),
                str(item.get("account_name") or ""),
                str(item.get("status") or ""),
                str(item.get("status_detail") or ""),
                item.get("current_price"),
                item.get("settle_price") if item.get("settle_price") is not None else item.get("settled_price"),
                item.get("suggested_price"),
                item.get("cost_price"),
                str(item.get("model") or ""),
                str(item.get("condition") or ""),
                str(item.get("capacity") or ""),
                str(item.get("imei") or ""),
                str(item.get("manual_review_state") or ""),
                str(item.get("manual_review_reason") or ""),
                str(item.get("op_status") or ""),
                str(item.get("op_message") or ""),
                json.dumps(item, ensure_ascii=False, default=str),
                row_hash,
            ))
        sql = (
            "INSERT INTO batch_items_snapshot "
            "(snapshot_ts,product_id,qc_code,account_name,status,status_detail,current_price,settle_price,suggested_price,cost_price,model,`condition`,capacity,imei,manual_review_state,manual_review_reason,op_status,op_message,raw_item_json,item_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE ingested_at=CURRENT_TIMESTAMP"
        )
        return self._safe_exec_many(sql, rows)

    def write_post_qc_intercepts(self, rows_in: Iterable[dict]) -> tuple[bool, str]:
        rows = []
        for row in rows_in:
            row_hash = self._hash_obj(row)
            event_time = self._parse_dt(row.get("event_time") or row.get("apply_return_time") or row.get("sold_time"))
            event_date = event_time.date() if event_time else None
            photos = list(row.get("flawed_photos") or [])
            rows.append((
                event_date,
                event_time,
                str(row.get("account_name") or ""),
                str(row.get("product_id") or ""),
                str(row.get("qc_code") or ""),
                str(row.get("imei") or ""),
                str(row.get("model") or ""),
                str(row.get("title") or ""),
                str(row.get("qc_item_name") or ""),
                str(row.get("ori_qc_result") or ""),
                str(row.get("post_qc_result") or ""),
                str(photos[0]) if photos else "",
                json.dumps(row, ensure_ascii=False, default=str),
                row_hash,
            ))
        sql = (
            "INSERT INTO post_qc_intercepts "
            "(event_date,event_time,account_name,product_id,qc_code,imei,model,title,qc_item_name,ori_qc_result,post_qc_result,photo_url,raw_row_json,row_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE ingested_at=CURRENT_TIMESTAMP"
        )
        return self._safe_exec_many(sql, rows)


    def load_sold_records(self) -> tuple[bool, str, list[dict]]:
        sql = (
            "SELECT product_id,title,sold_price,sold_time,hours_to_sell,source,model,`condition`,capacity,color,list_time,settle_price "
            "FROM sold_records ORDER BY sold_time DESC"
        )
        return self._safe_query(sql)

    def load_price_changes(self, *, days: int = 30, limit: int = 500) -> tuple[bool, str, list[dict]]:
        sql = (
            "SELECT source_id,timestamp_dt,product_id,qc_code,title,model,`condition`,capacity,color,old_price,new_price,diff,settle_price,`trigger`,account_name,note "
            "FROM price_changes WHERE timestamp_dt >= (NOW() - INTERVAL %s DAY) "
            "ORDER BY timestamp_dt DESC LIMIT %s"
        )
        return self._safe_query(sql, (int(days), int(limit)))

    def count_rows(self, table_name: str) -> tuple[bool, str, int]:
        table = str(table_name or "").strip()
        if table not in self._COUNTABLE_TABLES:
            return False, f"table not allowed: {table}", 0
        ok, msg, rows = self._safe_query(f"SELECT COUNT(*) AS c FROM {table}")
        if not ok:
            return False, msg, 0
        if not rows:
            return True, "ok", 0
        return True, "ok", int((rows[0] or {}).get("c") or 0)

    def count_rows_since(self, table_name: str, since_ts: datetime.datetime) -> tuple[bool, str, int]:
        table = str(table_name or "").strip()
        if table not in self._COUNTABLE_TABLES:
            return False, f"table not allowed: {table}", 0
        if not isinstance(since_ts, datetime.datetime):
            return False, "invalid since_ts", 0
        ok, msg, rows = self._safe_query(
            f"SELECT COUNT(*) AS c FROM {table} WHERE ingested_at >= %s",
            (since_ts,),
        )
        if not ok:
            return False, msg, 0
        if not rows:
            return True, "ok", 0
        return True, "ok", int((rows[0] or {}).get("c") or 0)


def get_mysql_store() -> MySQLStore:
    return MySQLStore.get()
