"""
core/batch_store.py — 线程安全的批量调价条目存储

替换原 self._batch_items 裸 list，所有读写操作均在锁内执行。
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import List, Optional, Callable

from .models import BatchItem, ProductStatus
from ..services.mysql_store import get_mysql_store


class BatchItemStore:
    """
    线程安全的 BatchItem 容器。

    UI线程和后台线程均通过此类的方法操作数据，
    不允许外部直接持有内部 list 的引用。
    """

    def __init__(self) -> None:
        self._items: List[BatchItem] = []
        self._lock = threading.Lock()

    # ── 写操作 ───────────────────────────────────

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def remove(self, product_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.product_id != product_id]
            return len(self._items) != before

    def extend(self, new_items: List[BatchItem]) -> int:
        """追加，自动去重（按 product_id），返回实际新增数量"""
        with self._lock:
            existing_ids = {it.product_id for it in self._items}
            added = [it for it in new_items if it.product_id not in existing_ids]
            self._items.extend(added)
            return len(added)

    def replace_all(self, items: List[BatchItem]) -> None:
        with self._lock:
            self._items = list(items)

    def set_all(self, items: List[BatchItem]) -> None:
        self.replace_all(items)

    def update_item(self, product_id: str, **kwargs) -> bool:
        """更新指定 product_id 的字段，返回是否找到"""
        with self._lock:
            for item in self._items:
                if item.product_id == product_id:
                    for k, v in kwargs.items():
                        if hasattr(item, k):
                            setattr(item, k, v)
                    return True
        return False

    def apply_to_all(self, func: Callable[[BatchItem], None]) -> None:
        """对所有条目应用函数（在锁内执行）"""
        with self._lock:
            for item in self._items:
                func(item)

    # ── 读操作（返回副本，避免外部持有引用）──────

    def snapshot(self) -> List[BatchItem]:
        """返回当前所有条目的浅拷贝列表"""
        with self._lock:
            return list(self._items)

    def get_all(self) -> List[BatchItem]:
        return self.snapshot()

    def filter_snapshot(self, predicate: Callable[[BatchItem], bool]) -> List[BatchItem]:
        with self._lock:
            return [it for it in self._items if predicate(it)]

    def on_sale_snapshot(self) -> List[BatchItem]:
        return self.filter_snapshot(lambda it: it.status == ProductStatus.ON_SALE)

    def get(self, product_id: str) -> Optional[BatchItem]:
        with self._lock:
            return next((it for it in self._items if it.product_id == product_id), None)

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def __len__(self) -> int:
        return self.count()

    def save_to_file(self, path: str) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = [self._item_to_dict(item) for item in self._items]
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            get_mysql_store().write_batch_snapshot(datetime.datetime.now(), payload)
        except Exception:
            pass

    def load_from_file(self, path: str) -> int:
        target = Path(path)
        if not target.exists():
            return 0
        raw = json.loads(target.read_text(encoding="utf-8") or "[]")
        if not isinstance(raw, list):
            return 0
        items: list[BatchItem] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            item = self._item_from_dict(row)
            if item is not None:
                items.append(item)
        with self._lock:
            self._items = items
        return len(items)

    @staticmethod
    def _parse_dt(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.datetime.fromisoformat(text)
        except Exception:
            return None

    @staticmethod
    def _status_from_text(value):
        text = str(value or "").strip()
        if not text:
            return ProductStatus.UNKNOWN
        for status in ProductStatus:
            if text in {status.value, status.label, status.name}:
                return status
        return ProductStatus.UNKNOWN

    @staticmethod
    def _status_to_text(status):
        if isinstance(status, ProductStatus):
            return status.value
        return str(getattr(status, "value", status) or ProductStatus.UNKNOWN.value)

    def _item_to_dict(self, item: BatchItem) -> dict:
        return {
            "product_id": item.product_id,
            "qc_code": item.qc_code,
            "title": item.title,
            "current_price": item.current_price,
            "status": self._status_to_text(item.status),
            "status_detail": getattr(item, "status_detail", ""),
            "account_name": item.account_name,
            "imei": getattr(item, "imei", ""),
            "model": item.model,
            "condition": item.condition,
            "capacity": item.capacity,
            "color": item.color,
            "cost_price": item.cost_price,
            "listed_time": item.listed_time.isoformat() if isinstance(item.listed_time, datetime.datetime) else None,
            "settled_price": item.settle_price,
            "settle_price": item.settle_price,
            "suggested_price": item.suggested_price,
            "suggested_settled_price": item.suggested_settle_price,
            "suggested_settle_price": item.suggested_settle_price,
            "selected": item.selected,
            "ignored": item.ignored,
            "manual_review_state": getattr(item, "manual_review_state", "none"),
            "manual_review_reason": getattr(item, "manual_review_reason", ""),
            "manual_review_source": getattr(item, "manual_review_source", ""),
            "manual_review_target_action": getattr(item, "manual_review_target_action", ""),
            "manual_review_target_price": getattr(item, "manual_review_target_price", None),
            "probe_anchor_price": getattr(item, "probe_anchor_price", None),
            "probe_last_side": getattr(item, "probe_last_side", ""),
            "probe_last_at": getattr(item, "probe_last_at", None).isoformat() if isinstance(getattr(item, "probe_last_at", None), datetime.datetime) else None,
            "probe_day": getattr(item, "probe_day", ""),
            "probe_day_count": getattr(item, "probe_day_count", 0),
            "op_status": getattr(item, "op_status", ""),
            "op_message": getattr(item, "op_message", ""),
            "reprice_msg": getattr(item, "reprice_msg", ""),
            "import_source": getattr(item, "import_source", ""),
        }

    def _item_from_dict(self, row: dict) -> Optional[BatchItem]:
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            return None
        import_source = str(row.get("import_source") or "").strip().lower() or "erp"
        item = BatchItem(
            product_id=product_id,
            qc_code=str(row.get("qc_code") or ""),
            title=str(row.get("title") or ""),
            current_price=float(row.get("current_price") or 0.0),
            status=self._status_from_text(row.get("status")),
            status_detail=str(row.get("status_detail") or ""),
            account_name=str(row.get("account_name") or ""),
            imei=str(row.get("imei") or ""),
            model=str(row.get("model") or ""),
            condition=str(row.get("condition") or ""),
            capacity=str(row.get("capacity") or ""),
            color=str(row.get("color") or ""),
            cost_price=float(row.get("cost_price") or 0.0),
            listed_time=self._parse_dt(row.get("listed_time")),
            settle_price=float(row.get("settled_price") if row.get("settled_price") not in (None, "") else (row.get("settle_price") or 0.0)),
            suggested_price=row.get("suggested_price"),
            suggested_settle_price=(row.get("suggested_settled_price") if row.get("suggested_settled_price") is not None else row.get("suggested_settle_price")),
            selected=bool(row.get("selected", True)),
            ignored=bool(row.get("ignored", False)),
            import_source=import_source,
        )
        item.manual_review_state = str(row.get("manual_review_state") or "none")
        item.manual_review_reason = str(row.get("manual_review_reason") or "")
        item.manual_review_source = str(row.get("manual_review_source") or "")
        item.manual_review_target_action = str(row.get("manual_review_target_action") or "")
        item.manual_review_target_price = row.get("manual_review_target_price")
        item.probe_anchor_price = row.get("probe_anchor_price")
        item.probe_last_side = str(row.get("probe_last_side") or "")
        item.probe_last_at = self._parse_dt(row.get("probe_last_at"))
        item.probe_day = str(row.get("probe_day") or "")
        item.probe_day_count = int(row.get("probe_day_count") or 0)
        item.op_status = str(row.get("op_status") or "")
        item.op_message = str(row.get("op_message") or "")
        item.reprice_msg = str(row.get("reprice_msg") or "")
        return item
