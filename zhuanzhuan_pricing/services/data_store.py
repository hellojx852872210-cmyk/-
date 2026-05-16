# -*- coding: utf-8 -*-
"""本地数据持久化"""
from __future__ import annotations

import csv
import json
import threading
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional

from ..config import (
    CACHE_FILE,
    CONFIG_FILE,
    ENGINE_MIN_SAMPLE,
    ERP_COST_MAP_FILE,
    LEGACY_PATHS,
    atomic_write_text,
    cfg,
    ensure_parent_dir,
    resolve_storage_path,
)
from ..core.models import Account, SoldRecord
from ..core.utils import clean_model_name, extract_capacity_text, extract_color_text, fuzzy_model_match
from .mysql_store import get_mysql_store


class AccountStore:
    def __init__(self, path=None):
        current_path = Path(path or CONFIG_FILE)
        legacy_path = None if path else LEGACY_PATHS["accounts"]
        self._current_path = current_path
        self._legacy_path = Path(legacy_path) if legacy_path else None
        self._path = resolve_storage_path(current_path, self._legacy_path)
        self._lock = threading.Lock()

    def _refresh_path(self) -> Path:
        self._path = resolve_storage_path(self._current_path, self._legacy_path)
        return self._path

    def load(self) -> List[Account]:
        path = self._refresh_path()
        if not path.exists():
            return []
        with self._lock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return [Account.from_dict(d) for d in data]
            except Exception:
                return []

    def save(self, accounts: List[Account]) -> None:
        payload = json.dumps([a.to_dict() for a in accounts], ensure_ascii=False, indent=2)
        with self._lock:
            target = atomic_write_text(self._current_path, payload)
            self._path = target
            if self._legacy_path and self._legacy_path.exists() and self._legacy_path.resolve() != target.resolve():
                try:
                    self._legacy_path.unlink()
                except Exception:
                    pass

    def load_all(self) -> List[Account]:
        return self.load()

    def save_all(self, accounts: List[Account]) -> None:
        self.save(accounts)

    def enabled_accounts(self) -> List[Account]:
        return [a for a in self.load() if a.enabled]


class SoldCacheStore:
    COLUMNS = [
        "product_id", "title", "sold_price", "sold_time", "hours_to_sell", "source",
        "model", "condition", "capacity", "color", "list_time", "settle_price",
    ]

    def __init__(self, path=None):
        current_path = Path(path or CACHE_FILE)
        legacy_path = None if path else LEGACY_PATHS["sold_cache"]
        self._current_path = current_path
        self._legacy_path = Path(legacy_path) if legacy_path else None
        self._path = resolve_storage_path(current_path, self._legacy_path)
        self._lock = threading.Lock()

    def _refresh_path(self) -> Path:
        self._path = resolve_storage_path(self._current_path, self._legacy_path)
        return self._path

    def _parse_float(self, value, default: Optional[float] = None) -> Optional[float]:
        if value in (None, ""):
            return default
        try:
            return float(value)
        except Exception:
            return default

    def _parse_datetime(self, value) -> Optional[datetime]:
        if value in (None, ""):
            return None
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    def _normalize_text(self, value: str) -> str:
        return str(value or "").strip()

    def _record_from_row(self, row: dict) -> Optional[SoldRecord]:
        try:
            title = self._normalize_text(row.get("title", ""))
            sold_time = self._parse_datetime(row.get("sold_time"))
            if sold_time is None:
                return None
            model = self._normalize_text(row.get("model", "")) or clean_model_name(title)
            return SoldRecord(
                product_id=self._normalize_text(row.get("product_id", "")),
                title=title,
                sold_price=float(row["sold_price"]),
                sold_time=sold_time,
                hours_to_sell=self._parse_float(row.get("hours_to_sell"), None),
                source=self._normalize_text(row.get("source", "zhuanzhuan")) or "zhuanzhuan",
                model=model,
                condition=self._normalize_text(row.get("condition", "")),
                capacity=self._normalize_text(row.get("capacity", "")),
                color=self._normalize_text(row.get("color", "")),
                list_time=self._parse_datetime(row.get("list_time")),
                settle_price=self._parse_float(row.get("settle_price"), None),
            )
        except Exception:
            return None

    def _load_from_mysql(self) -> List[SoldRecord]:
        ok, _msg, rows = get_mysql_store().load_sold_records()
        if not ok:
            return []
        records: list[SoldRecord] = []
        for row in rows:
            record = self._record_from_row({
                "product_id": row.get("product_id"),
                "title": row.get("title"),
                "sold_price": row.get("sold_price"),
                "sold_time": row.get("sold_time"),
                "hours_to_sell": row.get("hours_to_sell"),
                "source": row.get("source"),
                "model": row.get("model"),
                "condition": row.get("condition"),
                "capacity": row.get("capacity"),
                "color": row.get("color"),
                "list_time": row.get("list_time"),
                "settle_price": row.get("settle_price"),
            })
            if record is not None:
                records.append(record)
        return records

    def load(self) -> List[SoldRecord]:
        if bool(cfg.get("mysql_primary_read_enabled", False)):
            mysql_records = self._load_from_mysql()
            if mysql_records:
                return mysql_records
        path = self._refresh_path()
        if not path.exists():
            return []
        records = []
        with self._lock:
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        record = self._record_from_row(row)
                        if record is not None:
                            records.append(record)
            except Exception:
                return []
        return records

    def save(self, records: List[SoldRecord]) -> None:
        temp_rows: list[dict] = []
        for r in records:
            temp_rows.append({
                "product_id": r.product_id,
                "title": r.title,
                "sold_price": r.sold_price,
                "sold_time": r.sold_time.isoformat(),
                "hours_to_sell": r.hours_to_sell if r.hours_to_sell is not None else "",
                "source": r.source,
                "model": r.model,
                "condition": r.condition,
                "capacity": r.capacity,
                "color": r.color,
                "list_time": r.list_time.isoformat() if r.list_time else "",
                "settle_price": r.settle_price if r.settle_price is not None else "",
            })
        with self._lock:
            buffer = StringIO()
            writer = csv.DictWriter(buffer, fieldnames=self.COLUMNS)
            writer.writeheader()
            writer.writerows(temp_rows)
            target = atomic_write_text(self._current_path, buffer.getvalue())
            self._path = target
            if self._legacy_path and self._legacy_path.exists() and self._legacy_path.resolve() != target.resolve():
                try:
                    self._legacy_path.unlink()
                except Exception:
                    pass
        try:
            get_mysql_store().write_sold_records([
                {
                    "product_id": r.product_id,
                    "title": r.title,
                    "sold_price": r.sold_price,
                    "sold_time": r.sold_time.isoformat() if r.sold_time else "",
                    "hours_to_sell": r.hours_to_sell,
                    "source": r.source,
                    "model": r.model,
                    "condition": r.condition,
                    "capacity": r.capacity,
                    "color": r.color,
                    "list_time": r.list_time.isoformat() if r.list_time else "",
                    "settle_price": r.settle_price,
                }
                for r in records
            ])
        except Exception:
            pass

    def _merge_record(self, existing: SoldRecord, incoming: SoldRecord) -> SoldRecord:
        return SoldRecord(
            product_id=self._normalize_text(incoming.product_id) or existing.product_id,
            title=self._normalize_text(incoming.title) or existing.title,
            sold_price=incoming.sold_price if incoming.sold_price is not None else existing.sold_price,
            sold_time=incoming.sold_time or existing.sold_time,
            hours_to_sell=incoming.hours_to_sell if incoming.hours_to_sell is not None else existing.hours_to_sell,
            source=self._normalize_text(incoming.source) or existing.source,
            model=self._normalize_text(incoming.model) or existing.model,
            condition=self._normalize_text(incoming.condition) or existing.condition,
            capacity=self._normalize_text(incoming.capacity) or existing.capacity,
            color=self._normalize_text(incoming.color) or existing.color,
            list_time=incoming.list_time or existing.list_time,
            settle_price=incoming.settle_price if incoming.settle_price is not None else existing.settle_price,
        )

    def upsert(self, new_records: List[SoldRecord]) -> tuple[int, int]:
        existing = self.load()
        index_by_id = {r.product_id: i for i, r in enumerate(existing)}
        added = 0
        updated = 0
        for record in new_records:
            idx = index_by_id.get(record.product_id)
            if idx is None:
                index_by_id[record.product_id] = len(existing)
                existing.append(record)
                added += 1
                continue
            merged = self._merge_record(existing[idx], record)
            if merged != existing[idx]:
                existing[idx] = merged
                updated += 1
        if added or updated:
            self.save(existing)
        return added, updated

    def append(self, new_records: List[SoldRecord]) -> int:
        added, _updated = self.upsert(new_records)
        return added

    def clear(self) -> None:
        self.save([])

    def count(self) -> int:
        return len(self.load())

    def get_filter_options(
        self,
        model: str = "",
        condition: str = "",
        capacity: str = "",
        color: str = "",
        fuzzy_model: bool = False,
    ) -> dict[str, list[str]]:
        records = self.find_records(
            model=model,
            condition=condition,
            capacity=capacity,
            color=color,
            fuzzy_model=fuzzy_model,
        )
        return {
            "model": self._unique_values(records, "model"),
            "condition": self._unique_values(records, "condition"),
            "capacity": self._unique_values(records, "capacity"),
            "color": self._unique_values(records, "color"),
        }

    def _record_value(self, record: SoldRecord, field_name: str) -> str:
        value = self._normalize_text(getattr(record, field_name, ""))
        if value:
            return value
        title = self._normalize_text(record.title)
        if field_name == "model":
            return clean_model_name(title)
        if field_name == "capacity":
            return extract_capacity_text(title)
        if field_name == "color":
            return extract_color_text(title)
        return ""


    def _unique_values(self, records: List[SoldRecord], field_name: str) -> list[str]:
        values = {
            self._record_value(record, field_name)
            for record in records
            if self._record_value(record, field_name)
        }
        return sorted(values)

    def _mark_match(self, record: SoldRecord, weight: int = 1, tier: str = "default") -> SoldRecord:
        record.match_weight = max(1, int(weight or 1))
        record.match_tier = tier or "default"
        return record

    def _record_matches(
        self,
        record: SoldRecord,
        criteria: dict[str, str],
        fuzzy_model: bool = False,
    ) -> bool:
        title = self._normalize_text(record.title)
        for field_name, expected in criteria.items():
            if not expected:
                continue
            actual = self._record_value(record, field_name)

            if field_name == "model" and fuzzy_model:
                if actual and fuzzy_model_match(expected, actual):
                    continue
                if fuzzy_model_match(expected, title):
                    continue
                return False

            expected_lower = expected.lower()
            actual_lower = actual.lower()

            if actual:
                if actual_lower != expected_lower:
                    return False
                continue

            if field_name == "condition":
                return False

            if expected_lower not in title.lower():
                return False
        return True

    def _take_recent_matches(
        self,
        records: List[SoldRecord],
        weight: int,
        tier: str,
        limit: int,
    ) -> List[SoldRecord]:
        picked = sorted(records, key=lambda r: r.sold_time, reverse=True)
        if limit > 0:
            picked = picked[:limit]
        return [self._mark_match(record, weight, tier) for record in picked]

    def find_records(
        self,
        model: str = "",
        condition: str = "",
        capacity: str = "",
        color: str = "",
        fuzzy_model: bool = False,
    ) -> List[SoldRecord]:
        criteria = {
            "model": self._normalize_text(model),
            "condition": self._normalize_text(condition),
            "capacity": self._normalize_text(capacity),
            "color": self._normalize_text(color),
        }
        if not any(criteria.values()):
            return [self._mark_match(record, 1, "exact") for record in self.load()]
        records = [
            record
            for record in self.load()
            if self._record_matches(record, criteria, fuzzy_model=fuzzy_model)
        ]
        if not criteria["condition"]:
            return [self._mark_match(record, 1, "exact") for record in records]

        expected_lower = criteria["condition"].lower()
        exact_condition_records = []
        blank_condition_records = []

        for record in records:
            actual_condition = self._record_value(record, "condition").lower()
            if actual_condition == expected_lower:
                exact_condition_records.append(record)
                continue
            if not actual_condition:
                blank_condition_records.append(record)

        exact_matches = self._take_recent_matches(exact_condition_records, 10, "exact_condition", 0)
        if exact_matches:
            fallback_matches = self._take_recent_matches(blank_condition_records, 1, "blank_condition_fallback", 0)
            return exact_matches + fallback_matches
        if blank_condition_records:
            return self._take_recent_matches(blank_condition_records, 1, "blank_condition_fallback", 0)
        return []

    def query_records(
        self,
        model: str = "",
        condition: str = "",
        capacity: str = "",
        color: str = "",
        days: int = 0,
        keyword: str = "",
        limit: int = 1000,
        fuzzy_model: bool = False,
    ) -> List[SoldRecord]:
        records = self.filter_by_model_key(model, condition, capacity, color, fuzzy_model=fuzzy_model)
        keyword = self._normalize_text(keyword).lower()
        if days and days > 0:
            cutoff = datetime.now() - timedelta(days=days)
            records = [r for r in records if r.sold_time >= cutoff]
        if keyword:
            records = [
                r for r in records
                if keyword in (r.title or "").lower()
                or keyword in (r.model or "").lower()
                or keyword in (r.condition or "").lower()
                or keyword in (r.capacity or "").lower()
                or keyword in (r.color or "").lower()
            ]
        records.sort(key=lambda r: r.sold_time, reverse=True)
        if limit and limit > 0:
            records = records[:limit]
        return records

    def filter_by_model_key(
        self,
        model: str = "",
        condition: str = "",
        capacity: str = "",
        color: str = "",
        fuzzy_model: bool = False,
    ) -> List[SoldRecord]:
        return self.find_records(
            model=model,
            condition=condition,
            capacity=capacity,
            color=color,
            fuzzy_model=fuzzy_model,
        )


class CostPriceStore:
    def __init__(self, path=None):
        current_path = Path(path or ERP_COST_MAP_FILE)
        legacy_path = None if path else LEGACY_PATHS["erp_cost_map"]
        self._current_path = current_path
        self._legacy_path = Path(legacy_path) if legacy_path else None
        self._path = resolve_storage_path(current_path, self._legacy_path)
        self._lock = threading.Lock()
        self._data: Dict[str, float] = {}
        self.load()

    def _refresh_path(self) -> Path:
        self._path = resolve_storage_path(self._current_path, self._legacy_path)
        return self._path

    def load(self):
        path = self._refresh_path()
        with self._lock:
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        self._data = json.load(f)
                except Exception:
                    self._data = {}
        return dict(self._data)

    def save(self):
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        with self._lock:
            target = atomic_write_text(self._current_path, payload)
            self._path = target
            if self._legacy_path and self._legacy_path.exists() and self._legacy_path.resolve() != target.resolve():
                try:
                    self._legacy_path.unlink()
                except Exception:
                    pass

    def get(self, product_id: str) -> float:
        return float(self._data.get(str(product_id), 0.0) or 0.0)

    def update(self, mapping: Dict[str, float]):
        with self._lock:
            self._data.update({str(k): float(v) for k, v in mapping.items()})
        self.save()


SoldCache = SoldCacheStore
CostPriceMap = CostPriceStore
