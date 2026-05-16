# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import threading
from typing import List, Optional

from ..config import LEGACY_PATHS, SAME_SALE_FILE, JsonConfigFile


@dataclass
class SameSaleListing:
    platform: str
    platform_label: str
    product_id: str = ""
    qc_code: str = ""
    imei: str = ""
    title: str = ""
    account_name: str = ""
    status: str = "待确认"
    price: float = 0.0
    note: str = ""
    sold: bool = False
    delist_pending: bool = False
    delisted: bool = False
    sold_at: str = ""
    updated_at: str = ""


@dataclass
class SameSaleGroup:
    group_id: str
    machine_code: str = ""
    imei: str = ""
    model: str = ""
    condition: str = ""
    capacity: str = ""
    color: str = ""
    note: str = ""
    listings: List[SameSaleListing] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def touch(self):
        self.updated_at = datetime.now().isoformat(timespec="seconds")


class SameSaleStore:
    def __init__(self, path: str = SAME_SALE_FILE):
        legacy_path = None if path != SAME_SALE_FILE else LEGACY_PATHS.get("same_sale")
        self._file = JsonConfigFile(path, legacy_path)
        self.path = self._file.path
        self._lock = threading.RLock()
        self._groups: List[SameSaleGroup] = []
        self.load()

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _normalize_code(self, value: str) -> str:
        return str(value or "").strip()

    def _normalize_key(self, value: str) -> str:
        return self._normalize_code(value).lower()

    def _listing_from_dict(self, data: dict) -> SameSaleListing:
        return SameSaleListing(
            platform=str(data.get("platform", "") or "").strip(),
            platform_label=str(data.get("platform_label", "") or "").strip(),
            product_id=str(data.get("product_id", "") or "").strip(),
            qc_code=str(data.get("qc_code", "") or "").strip(),
            imei=str(data.get("imei", "") or "").strip(),
            title=str(data.get("title", "") or "").strip(),
            account_name=str(data.get("account_name", "") or "").strip(),
            status=str(data.get("status", "待确认") or "待确认").strip(),
            price=float(data.get("price") or 0.0),
            note=str(data.get("note", "") or "").strip(),
            sold=bool(data.get("sold", False)),
            delist_pending=bool(data.get("delist_pending", False)),
            delisted=bool(data.get("delisted", False)),
            sold_at=str(data.get("sold_at", "") or "").strip(),
            updated_at=str(data.get("updated_at", "") or self._now()).strip(),
        )

    def _group_from_dict(self, data: dict) -> SameSaleGroup:
        listings = [self._listing_from_dict(item) for item in data.get("listings", [])]
        return SameSaleGroup(
            group_id=str(data.get("group_id", "") or "").strip(),
            machine_code=str(data.get("machine_code", "") or "").strip(),
            imei=str(data.get("imei", "") or "").strip(),
            model=str(data.get("model", "") or "").strip(),
            condition=str(data.get("condition", "") or "").strip(),
            capacity=str(data.get("capacity", "") or "").strip(),
            color=str(data.get("color", "") or "").strip(),
            note=str(data.get("note", "") or "").strip(),
            listings=listings,
            created_at=str(data.get("created_at", "") or self._now()).strip(),
            updated_at=str(data.get("updated_at", "") or self._now()).strip(),
        )

    def load(self) -> List[SameSaleGroup]:
        with self._lock:
            raw = self._file.load(list)
            self.path = self._file.path
            self._groups = [self._group_from_dict(item) for item in raw if isinstance(item, dict)]
            return self.snapshot()

    def save(self) -> None:
        with self._lock:
            self._file.save([asdict(group) for group in self._groups])
            self.path = self._file.path

    def snapshot(self) -> List[SameSaleGroup]:
        with self._lock:
            return [self._group_from_dict(asdict(group)) for group in self._groups]

    def _next_group_id(self) -> str:
        return f"SS-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def get_all(self) -> List[SameSaleGroup]:
        return self.snapshot()

    def count(self) -> int:
        with self._lock:
            return len(self._groups)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for group in self._groups for listing in group.listings if listing.delist_pending)

    def sold_count(self) -> int:
        with self._lock:
            return sum(1 for group in self._groups for listing in group.listings if listing.sold)

    def add_group(
        self,
        *,
        machine_code: str = "",
        imei: str = "",
        model: str = "",
        condition: str = "",
        capacity: str = "",
        color: str = "",
        note: str = "",
    ) -> SameSaleGroup:
        with self._lock:
            group = SameSaleGroup(
                group_id=self._next_group_id(),
                machine_code=self._normalize_code(machine_code),
                imei=self._normalize_code(imei),
                model=str(model or "").strip(),
                condition=str(condition or "").strip(),
                capacity=str(capacity or "").strip(),
                color=str(color or "").strip(),
                note=str(note or "").strip(),
            )
            self._groups.append(group)
            self._file.save([asdict(item) for item in self._groups])
            return self._group_from_dict(asdict(group))

    def delete_group(self, group_id: str) -> bool:
        with self._lock:
            before = len(self._groups)
            self._groups = [group for group in self._groups if group.group_id != group_id]
            if len(self._groups) == before:
                return False
            self._file.save([asdict(item) for item in self._groups])
            return True

    def _find_group(self, group_id: str) -> Optional[SameSaleGroup]:
        for group in self._groups:
            if group.group_id == group_id:
                return group
        return None

    def add_listing(self, group_id: str, listing: SameSaleListing) -> Optional[SameSaleGroup]:
        with self._lock:
            group = self._find_group(group_id)
            if group is None:
                return None
            listing.platform = str(listing.platform or "").strip().lower()
            listing.platform_label = str(listing.platform_label or listing.platform or "").strip()
            listing.product_id = self._normalize_code(listing.product_id)
            listing.qc_code = self._normalize_code(listing.qc_code)
            listing.imei = self._normalize_code(listing.imei)
            listing.updated_at = self._now()
            group.listings.append(listing)
            group.touch()
            self._file.save([asdict(item) for item in self._groups])
            return self._group_from_dict(asdict(group))

    def update_group_note(self, group_id: str, note: str) -> bool:
        with self._lock:
            group = self._find_group(group_id)
            if group is None:
                return False
            group.note = str(note or "").strip()
            group.touch()
            self._file.save([asdict(item) for item in self._groups])
            return True

    def sync_imported_item(self, item) -> SameSaleGroup:
        machine_code = self._normalize_code(getattr(item, "qc_code", ""))
        imei = self._normalize_code(getattr(item, "imei", ""))
        listing = SameSaleListing(
            platform="zhuanzhuan",
            platform_label="转转",
            product_id=self._normalize_code(getattr(item, "product_id", "")),
            qc_code=machine_code,
            imei=imei,
            title=str(getattr(item, "title", "") or "").strip(),
            account_name=str(getattr(item, "account_name", "") or "").strip(),
            status=getattr(getattr(item, "status", None), "label", None) or str(getattr(item, "op_status", "") or "在库").strip(),
            price=float(getattr(item, "current_price", 0.0) or 0.0),
            sold=getattr(getattr(item, "status", None), "value", "") == "80",
        )
        with self._lock:
            group = self._find_group_by_identity(machine_code=machine_code, imei=imei)
            if group is None:
                group = SameSaleGroup(
                    group_id=self._next_group_id(),
                    machine_code=machine_code,
                    imei=imei,
                    model=str(getattr(item, "model", "") or "").strip(),
                    condition=str(getattr(item, "condition", "") or "").strip(),
                    capacity=str(getattr(item, "capacity", "") or "").strip(),
                    color=str(getattr(item, "color", "") or "").strip(),
                )
                self._groups.append(group)
            else:
                if not group.machine_code and machine_code:
                    group.machine_code = machine_code
                if not group.imei and imei:
                    group.imei = imei
                if not group.model:
                    group.model = str(getattr(item, "model", "") or "").strip()
                if not group.condition:
                    group.condition = str(getattr(item, "condition", "") or "").strip()
                if not group.capacity:
                    group.capacity = str(getattr(item, "capacity", "") or "").strip()
                if not group.color:
                    group.color = str(getattr(item, "color", "") or "").strip()

            existing = self._find_listing(group, listing.platform, listing.product_id, listing.qc_code, listing.imei)
            if existing is None:
                group.listings.append(listing)
            else:
                existing.product_id = listing.product_id or existing.product_id
                existing.qc_code = listing.qc_code or existing.qc_code
                existing.imei = listing.imei or existing.imei
                existing.title = listing.title or existing.title
                existing.account_name = listing.account_name or existing.account_name
                existing.status = listing.status or existing.status
                existing.price = listing.price
                existing.sold = listing.sold
                existing.updated_at = self._now()
            group.touch()
            self._file.save([asdict(item) for item in self._groups])
            return self._group_from_dict(asdict(group))

    def _find_group_by_identity(self, *, machine_code: str = "", imei: str = "") -> Optional[SameSaleGroup]:
        machine_key = self._normalize_key(machine_code)
        imei_key = self._normalize_key(imei)
        for group in self._groups:
            if machine_key and self._normalize_key(group.machine_code) == machine_key:
                return group
            if imei_key and self._normalize_key(group.imei) == imei_key:
                return group
            for listing in group.listings:
                if machine_key and self._normalize_key(listing.qc_code) == machine_key:
                    return group
                if imei_key and self._normalize_key(listing.imei) == imei_key:
                    return group
        return None

    def _find_listing(self, group: SameSaleGroup, platform: str, product_id: str, qc_code: str, imei: str) -> Optional[SameSaleListing]:
        platform_key = str(platform or "").strip().lower()
        product_key = self._normalize_key(product_id)
        qc_key = self._normalize_key(qc_code)
        imei_key = self._normalize_key(imei)
        for listing in group.listings:
            if str(listing.platform or "").strip().lower() != platform_key:
                continue
            if product_key and self._normalize_key(listing.product_id) == product_key:
                return listing
            if qc_key and self._normalize_key(listing.qc_code) == qc_key:
                return listing
            if imei_key and self._normalize_key(listing.imei) == imei_key:
                return listing
        return None

    def mark_sold(self, group_id: str, platform: str, product_id: str = "", qc_code: str = "", imei: str = "") -> bool:
        with self._lock:
            group = self._find_group(group_id)
            if group is None:
                return False
            listing = self._find_listing(group, platform, product_id, qc_code, imei)
            if listing is None:
                return False
            listing.sold = True
            listing.delist_pending = False
            listing.delisted = False
            listing.sold_at = self._now()
            listing.status = "已售"
            listing.updated_at = self._now()
            for other in group.listings:
                if other is listing:
                    continue
                if other.delisted:
                    continue
                other.delist_pending = True
                if not other.status or other.status == "待确认":
                    other.status = "待下架"
                other.updated_at = self._now()
            group.touch()
            self._file.save([asdict(item) for item in self._groups])
            return True

    def mark_delisted(self, group_id: str, platform: str, product_id: str = "", qc_code: str = "", imei: str = "") -> bool:
        with self._lock:
            group = self._find_group(group_id)
            if group is None:
                return False
            listing = self._find_listing(group, platform, product_id, qc_code, imei)
            if listing is None:
                return False
            listing.delist_pending = False
            listing.delisted = True
            listing.status = "已下架"
            listing.updated_at = self._now()
            group.touch()
            self._file.save([asdict(item) for item in self._groups])
            return True

    def clear_pending(self, group_id: str, platform: str, product_id: str = "", qc_code: str = "", imei: str = "") -> bool:
        with self._lock:
            group = self._find_group(group_id)
            if group is None:
                return False
            listing = self._find_listing(group, platform, product_id, qc_code, imei)
            if listing is None:
                return False
            listing.delist_pending = False
            if listing.status == "待下架":
                listing.status = "在库"
            listing.updated_at = self._now()
            group.touch()
            self._file.save([asdict(item) for item in self._groups])
            return True
