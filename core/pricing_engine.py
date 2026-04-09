# -*- coding: utf-8 -*-
"""定价引擎"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import List, Optional

from ..config import (
    ENGINE_MIN_SAMPLE,
    ENGINE_NEAR,
    ENGINE_W_OLD,
    ENGINE_W_RECENT,
    ENGINE_WINDOW,
    FAST_SALE_HOURS,
    PLATFORM_FEE_RATE,
    STATION_SERVICE_FEE,
    cfg,
)
from .models import BatchItem, ConfidenceLevel, PricingResult, PricingRule, RuleActionType, SoldRecord


class PricingEngine:
    def __init__(self, platform_fee_rate=None, station_fee=None):
        self.platform_fee_rate = platform_fee_rate or getattr(cfg, "platform_fee_rate", PLATFORM_FEE_RATE)
        self.station_fee = station_fee or getattr(cfg, "station_service_fee", STATION_SERVICE_FEE)
        self.window_days = ENGINE_WINDOW
        self.near_days = ENGINE_NEAR
        self.w_recent = ENGINE_W_RECENT
        self.w_old = ENGINE_W_OLD
        self.min_sample = getattr(cfg, "engine_min_sample", ENGINE_MIN_SAMPLE)
        self.fast_hours = getattr(cfg, "fast_sale_hours", FAST_SALE_HOURS)

    def calculate(self, *args) -> PricingResult:
        if len(args) >= 2 and isinstance(args[0], list):
            records = args[0]
            title = " ".join(str(v).strip() for v in args[1:] if v and str(v).strip()) or "查询结果"
        elif len(args) >= 2:
            title = str(args[0])
            records = args[1]
        else:
            raise TypeError("calculate() expects either (title, records) or (records, model, condition, capacity, color)")

        now = datetime.now()
        cutoff = now - timedelta(days=self.window_days)
        near_cutoff = now - timedelta(days=self.near_days)
        window_records = [r for r in records if r.sold_time >= cutoff]

        if not window_records:
            return PricingResult(
                title=title,
                sample_count=0,
                fast_price=None,
                conservative_price=None,
                floor_price=None,
                recommended_price=None,
                settle_price=None,
                confidence=ConfidenceLevel.NONE,
                raw_records=[],
                warning="暂无成交样本",
            )

        recent_records = [r for r in window_records if r.sold_time >= near_cutoff]
        use_fallback_records = len(recent_records) < self.min_sample
        pricing_records = window_records if use_fallback_records else recent_records

        weighted = []
        weighted_settle = []
        for r in pricing_records:
            w = self._record_weight(r, now, allow_older=use_fallback_records)
            weighted.extend([r.sold_price] * w)
            if r.settle_price is not None:
                weighted_settle.extend([r.settle_price] * w)
        weighted.sort()
        weighted_settle.sort()

        fast_records = [
            r for r in pricing_records
            if r.hours_to_sell is not None and r.hours_to_sell <= self.fast_hours
        ]
        fast_price = None
        fast_settle = None
        if fast_records:
            fast_weighted = []
            fast_weighted_settle = []
            for r in fast_records:
                w = self._record_weight(r, now, allow_older=use_fallback_records)
                fast_weighted.extend([r.sold_price] * w)
                if r.settle_price is not None:
                    fast_weighted_settle.extend([r.settle_price] * w)
            fast_weighted.sort()
            fast_weighted_settle.sort()
            fast_price = self._percentile(fast_weighted, 50)
            fast_settle = self._percentile(fast_weighted_settle, 50)

        conservative_price = self._percentile(weighted, 40)
        conservative_settle = self._percentile(weighted_settle, 40)
        floor_price = self._percentile(weighted, 20)
        recommended = fast_price if fast_price is not None else conservative_price
        if fast_price is not None:
            settle = fast_settle if fast_settle is not None else self.calc_settle_price(fast_price)
        else:
            settle = (
                conservative_settle
                if conservative_settle is not None else self.calc_settle_price(conservative_price)
            ) if conservative_price is not None else None

        exact_sample_count = sum(1 for r in pricing_records if getattr(r, "match_tier", "") == "exact_condition")
        fallback_sample_count = sum(
            1 for r in pricing_records
            if getattr(r, "match_tier", "") and getattr(r, "match_tier", "") != "exact_condition"
        )

        warning_parts = []
        if len(recent_records) < self.min_sample:
            confidence = ConfidenceLevel.LOW
            warning_parts.append(f"近{self.near_days}天样本不足，已回退参考更早成交")
        else:
            confidence = ConfidenceLevel.HIGH
            warning_parts.append(f"近{self.near_days}天样本优先，越近权重越高")
        if exact_sample_count and fallback_sample_count:
            warning_parts.append(f"精确样本 {exact_sample_count}，补充样本 {fallback_sample_count}")

        return PricingResult(
            title=title,
            sample_count=len(pricing_records),
            fast_price=fast_price,
            conservative_price=conservative_price,
            floor_price=floor_price,
            recommended_price=recommended,
            settle_price=settle,
            confidence=confidence,
            raw_records=list(pricing_records),
            warning="；".join(warning_parts),
            exact_sample_count=exact_sample_count,
            fallback_sample_count=fallback_sample_count,
        )

    def apply_rules(self, item: BatchItem, result: PricingResult, rules: List[PricingRule]) -> PricingResult:
        for rule in sorted([r for r in rules if r.enabled], key=lambda r: r.priority):
            if not rule.match.matches(item):
                continue
            base = result.recommended_price
            if base is None:
                continue
            action = rule.action
            if action.type == RuleActionType.FAST_PRICE:
                next_price = round((result.fast_price or base) * (1 + action.adjust_pct / 100))
            elif action.type == RuleActionType.CONSERVATIVE_PRICE:
                next_price = round((result.conservative_price or base) * (1 + action.adjust_pct / 100))
            elif action.type == RuleActionType.FIXED_DROP:
                next_price = round(base - action.amount)
            elif action.type == RuleActionType.PCT_DROP:
                next_price = round(base * (1 - action.adjust_pct / 100))
            elif action.type == RuleActionType.FLOOR_PRICE:
                next_price = result.floor_price or base
            else:
                continue

            if result.floor_price is not None and next_price < result.floor_price:
                next_price = result.floor_price

            result.recommended_price = next_price
            result.settle_price = self.calc_settle_price(next_price)
            result.rule_hit = rule.name
            return result
        return result

    @staticmethod
    def calc_settle_price(price: Optional[float]) -> Optional[float]:
        if price is None:
            return None
        fee_rate = getattr(cfg, "platform_fee_rate", PLATFORM_FEE_RATE)
        station_fee = getattr(cfg, "station_service_fee", STATION_SERVICE_FEE)
        return round(price * (1 - fee_rate) - station_fee, 2)

    def _record_weight(self, record: SoldRecord, now: datetime, allow_older: bool) -> int:
        days_ago = max((now - record.sold_time).total_seconds() / 86400, 0)
        base_weight = getattr(record, "match_weight", 1) or 1
        if allow_older:
            half_life_days = max(self.near_days / 2, 1)
        else:
            half_life_days = max(self.near_days / 3, 1)
        decay_weight = math.exp(-days_ago / half_life_days)
        return max(1, round(base_weight * 8 * decay_weight))

    @staticmethod
    def _percentile(sorted_data, pct):
        if not sorted_data:
            return None
        n = len(sorted_data)
        idx = (pct / 100) * (n - 1)
        lo = int(idx)
        hi = lo + 1
        if hi >= n:
            return sorted_data[lo]
        return sorted_data[lo] * (1 - (idx - lo)) + sorted_data[hi] * (idx - lo)

    @staticmethod
    def build_records_from_df(df) -> List[SoldRecord]:
        records = []
        for _, row in df.iterrows():
            try:
                sold_time = row["sold_time"]
                if not isinstance(sold_time, datetime):
                    sold_time = datetime.fromisoformat(str(sold_time))
                records.append(SoldRecord(
                    product_id=str(row.get("product_id", "")),
                    title=str(row.get("title", "")),
                    sold_price=float(row["sold_price"]),
                    sold_time=sold_time,
                    hours_to_sell=float(row["hours_to_sell"]) if "hours_to_sell" in row and row["hours_to_sell"] == row["hours_to_sell"] else None,
                    source=str(row.get("source", "zhuanzhuan")),
                    model=str(row.get("model", "") or ""),
                    condition=str(row.get("condition", "") or ""),
                    capacity=str(row.get("capacity", "") or ""),
                    color=str(row.get("color", "") or ""),
                    list_time=(datetime.fromisoformat(str(row.get("list_time"))) if row.get("list_time") else None),
                    settle_price=(float(row.get("settle_price")) if row.get("settle_price") == row.get("settle_price") and row.get("settle_price") not in (None, "") else None),
                ))
            except Exception:
                continue
        return records
