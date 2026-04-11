# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class ItemStatus(Enum):
    ON_SALE = "0"
    OFF_SALE = "1"
    NOT_LISTED = "60"
    SOLD = "80"
    IN_QC = "99"
    UNKNOWN = "-1"

    @property
    def label(self) -> str:
        return {
            ItemStatus.ON_SALE: "在售",
            ItemStatus.OFF_SALE: "已下架",
            ItemStatus.NOT_LISTED: "未上架",
            ItemStatus.SOLD: "已售",
            ItemStatus.IN_QC: "质检中",
            ItemStatus.UNKNOWN: "未知",
        }[self]

    @classmethod
    def from_raw(cls, raw) -> "ItemStatus":
        s = str(raw).strip()
        for member in cls:
            if member.value == s:
                return member
        return cls.UNKNOWN


ProductStatus = ItemStatus


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    LOW = "low"
    NONE = "none"


class PriceTrigger(Enum):
    MANUAL = "manual"
    AUTO_REPRICE = "auto_reprice"
    AUTO_STALE = "auto_stale"
    AUTO_LIST = "auto_list"
    WXAPP_CMD = "wxapp_cmd"


class RuleActionType(Enum):
    FAST_PRICE = "fast_price"
    CONSERVATIVE_PRICE = "conservative_price"
    FIXED_DROP = "fixed_drop"
    PCT_DROP = "pct_drop"
    FLOOR_PRICE = "floor_price"


@dataclass(init=False)
class Account:
    name: str
    cookie: str
    note: str
    enabled: bool

    def __init__(self, name: str, cookie: str, note: str = "", remark: str = "", enabled: bool = True):
        self.name = name
        self.cookie = cookie
        self.note = note or remark or ""
        self.enabled = enabled

    @property
    def remark(self) -> str:
        return self.note

    @remark.setter
    def remark(self, value: str):
        self.note = value or ""

    def to_dict(self):
        return {
            "name": self.name,
            "cookie": self.cookie,
            "note": self.note,
            "remark": self.note,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d.get("name", ""),
            cookie=d.get("cookie", ""),
            note=d.get("note", d.get("remark", "")),
            enabled=d.get("enabled", True),
        )


@dataclass(init=False)
class ProductDetail:
    product_id: str
    qc_code: str
    title: str
    current_price: float
    status: ItemStatus
    status_text: str
    imei: str
    model: str
    condition: str
    capacity: str
    color: str
    listed_time: Optional[datetime]
    account_name: str
    cost_price: float
    settle_price: float

    def __init__(
        self,
        product_id: str,
        qc_code: str,
        title: str,
        status: ItemStatus,
        current_price: Optional[float] = None,
        price: Optional[float] = None,
        imei: str = "",
        model: str = "",
        condition: str = "",
        capacity: str = "",
        color: str = "",
        listed_time: Optional[datetime] = None,
        list_time: Optional[datetime] = None,
        account_name: str = "",
        cost_price: float = 0.0,
        settle_price: float = 0.0,
        status_text: str = "",
    ):
        self.product_id = product_id
        self.qc_code = qc_code
        self.title = title
        self.current_price = float(current_price if current_price is not None else (price or 0.0))
        self.status = status
        self.status_text = status_text or getattr(status, "label", "") or str(getattr(status, "value", status) or "")
        self.imei = imei
        self.model = model
        self.condition = condition
        self.capacity = capacity
        self.color = color
        self.listed_time = listed_time or list_time
        self.account_name = account_name
        self.cost_price = float(cost_price or 0.0)
        self.settle_price = float(settle_price or 0.0)

    @property
    def price(self) -> float:
        return self.current_price

    @price.setter
    def price(self, value: float):
        self.current_price = float(value or 0.0)

    @property
    def list_time(self) -> Optional[datetime]:
        return self.listed_time

    @list_time.setter
    def list_time(self, value: Optional[datetime]):
        self.listed_time = value

    @property
    def days_on_sale(self) -> int:
        if self.listed_time is None:
            return 0
        return (datetime.now() - self.listed_time).days

    @property
    def stale_days(self) -> int:
        return self.days_on_sale


@dataclass
class SoldRecord:
    product_id: str
    title: str
    sold_price: float
    sold_time: datetime
    hours_to_sell: Optional[float] = None
    source: str = "zhuanzhuan"
    model: str = ""
    condition: str = ""
    capacity: str = ""
    color: str = ""
    list_time: Optional[datetime] = None
    settle_price: Optional[float] = None
    match_weight: int = 1
    match_tier: str = "default"


@dataclass
class PricingResult:
    title: str = ""
    sample_count: int = 0
    fast_price: Optional[float] = None
    conservative_price: Optional[float] = None
    floor_price: Optional[float] = None
    market_floor_price: Optional[float] = None
    cost_floor_price: Optional[float] = None
    market_cap_price: Optional[float] = None
    market_base_price: Optional[float] = None
    age_discount_factor: float = 1.0
    age_adjusted_price: Optional[float] = None
    pre_rule_price: Optional[float] = None
    recommended_price: Optional[float] = None
    settle_price: Optional[float] = None
    confidence: ConfidenceLevel | str = ConfidenceLevel.NONE
    rule_hit: str = ""
    raw_records: List[SoldRecord] = field(default_factory=list)
    warning: str = ""
    exact_sample_count: int = 0
    fallback_sample_count: int = 0

    @property
    def cons_price(self) -> Optional[float]:
        return self.conservative_price

    @cons_price.setter
    def cons_price(self, value: Optional[float]):
        self.conservative_price = value

    def suggest_price(self) -> Optional[float]:
        return self.recommended_price


@dataclass
class BatchItem:
    product_id: str
    qc_code: str
    title: str
    current_price: float = 0.0
    status: ItemStatus = ItemStatus.UNKNOWN
    status_detail: str = ""
    account_name: str = ""
    imei: str = ""
    model: str = ""
    condition: str = ""
    capacity: str = ""
    color: str = ""
    cost_price: float = 0.0
    listed_time: Optional[datetime] = None
    suggested_price: Optional[float] = None
    floor_price: Optional[float] = None
    settle_price: Optional[float] = None
    suggested_settle_price: Optional[float] = None
    confidence: str = ""
    rule_hit: str = ""
    selected: bool = True
    category: str = ""
    ignored: bool = False
    op_status: str = "待处理"
    op_message: str = ""
    pricing: Optional[PricingResult] = None
    reprice_ok: Optional[bool] = None
    reprice_msg: str = ""
    new_price: Optional[float] = None

    @property
    def suggest_price(self) -> Optional[float]:
        return self.suggested_price

    @suggest_price.setter
    def suggest_price(self, value: Optional[float]):
        self.suggested_price = value

    @property
    def list_time(self) -> Optional[datetime]:
        return self.listed_time

    @list_time.setter
    def list_time(self, value: Optional[datetime]):
        self.listed_time = value

    @property
    def price_diff(self):
        if self.suggested_price is None:
            return None
        return self.suggested_price - self.current_price

    @property
    def days_on_sale(self) -> int:
        if self.listed_time is None:
            return 0
        return (datetime.now() - self.listed_time).days

    @property
    def stale_days(self) -> int:
        return self.days_on_sale


@dataclass
class PriceChangeRecord:
    product_id: str
    qc_code: str
    title: str
    model: str
    condition: str
    old_price: float
    new_price: float
    settle_price: float
    trigger: PriceTrigger | str
    account_name: str
    timestamp: datetime = field(default_factory=datetime.now)
    rule_hit: str = ""
    id: Optional[int] = None
    capacity: str = ""
    color: str = ""
    diff: Optional[float] = None
    note: str = ""

    def __post_init__(self):
        if self.diff is None:
            self.diff = self.new_price - self.old_price

    @property
    def diff_pct(self) -> float:
        if not self.old_price:
            return 0.0
        return (self.diff or 0.0) / self.old_price * 100


@dataclass
class RuleMatch:
    model_contains: Optional[str] = None
    condition_in: Optional[List[str]] = None
    capacity_in: Optional[List[str]] = None
    stale_days_gte: Optional[int] = None
    stale_days_lt: Optional[int] = None
    cost_gte: Optional[float] = None
    cost_lt: Optional[float] = None

    def matches(self, item: BatchItem) -> bool:
        if self.model_contains and self.model_contains not in (item.model or ""):
            return False
        if self.condition_in and item.condition not in self.condition_in:
            return False
        if self.capacity_in and item.capacity not in self.capacity_in:
            return False
        days = item.days_on_sale
        if self.stale_days_gte is not None and days < self.stale_days_gte:
            return False
        if self.stale_days_lt is not None and days >= self.stale_days_lt:
            return False
        if self.cost_gte is not None and item.cost_price < self.cost_gte:
            return False
        if self.cost_lt is not None and item.cost_price >= self.cost_lt:
            return False
        return True

    @classmethod
    def from_dict(cls, d):
        valid = {
            "model_contains", "condition_in", "capacity_in",
            "stale_days_gte", "stale_days_lt", "cost_gte", "cost_lt",
        }
        return cls(**{k: v for k, v in d.items() if k in valid})


@dataclass
class RuleAction:
    type: RuleActionType
    adjust_pct: float = 0.0
    amount: float = 0.0

    @classmethod
    def from_dict(cls, d):
        return cls(
            type=RuleActionType(d["type"]),
            adjust_pct=d.get("adjust_pct", 0.0),
            amount=d.get("amount", 0.0),
        )


@dataclass
class PricingRule:
    name: str
    priority: int
    enabled: bool
    match: RuleMatch
    action: RuleAction
    note: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d["name"],
            priority=d.get("priority", 50),
            enabled=d.get("enabled", True),
            match=RuleMatch.from_dict(d.get("match", {})),
            action=RuleAction.from_dict(d["action"]),
            note=d.get("note", ""),
        )

    def to_dict(self):
        return {
            "name": self.name,
            "priority": self.priority,
            "enabled": self.enabled,
            "match": {k: v for k, v in self.match.__dict__.items() if v is not None},
            "action": {
                "type": self.action.type.value,
                "adjust_pct": self.action.adjust_pct,
                "amount": self.action.amount,
            },
            "note": self.note,
        }
