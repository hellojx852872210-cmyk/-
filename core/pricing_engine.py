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
        short_days = min(14, max(7, self.near_days // 2))
        short_cutoff = now - timedelta(days=short_days)
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
        short_records = [r for r in recent_records if r.sold_time >= short_cutoff]
        use_fallback_records = len(recent_records) < self.min_sample
        pricing_records = window_records if use_fallback_records else recent_records
        active_short_records = [r for r in pricing_records if r.sold_time >= short_cutoff]
        if not active_short_records:
            active_short_records = pricing_records

        weighted, weighted_settle = self._build_weighted_series(pricing_records, now, allow_older=use_fallback_records)
        short_weighted, short_weighted_settle = self._build_weighted_series(
            active_short_records,
            now,
            allow_older=use_fallback_records,
            short_window=True,
        )

        fast_records = [
            r for r in pricing_records
            if r.hours_to_sell is not None and r.hours_to_sell <= self.fast_hours
        ]
        short_fast_records = [
            r for r in active_short_records
            if r.hours_to_sell is not None and r.hours_to_sell <= self.fast_hours
        ]
        fast_pool = short_fast_records or fast_records
        fast_weighted, fast_weighted_settle = self._build_weighted_series(
            fast_pool,
            now,
            allow_older=use_fallback_records,
            short_window=True,
        )

        long_median = self._percentile(weighted, 50)
        short_median = self._percentile(short_weighted, 50)
        short_anchor = self._percentile(short_weighted, 45)
        conservative_base = self._percentile(weighted, 40)
        floor_price = self._percentile(weighted, 20)
        fast_price = self._percentile(fast_weighted, 50)
        fast_settle = self._percentile(fast_weighted_settle, 50)
        conservative_settle = self._percentile(weighted_settle, 40)
        short_settle = self._percentile(short_weighted_settle, 45)

        conservative_price = self._blend_prices(short_anchor, conservative_base, primary_ratio=0.7)
        trend_adjust_pct = self._trend_adjust_pct(short_median, long_median)
        market_floor_price = floor_price
        market_base_price = self._blend_prices(conservative_price, fast_price, primary_ratio=0.6)
        age_discount_factor = 1.0
        age_adjusted_price = market_base_price
        recommended_seed = self._blend_prices(fast_price, conservative_price, primary_ratio=0.7)
        recommended = self._apply_trend_adjustment(recommended_seed, trend_adjust_pct)
        if recommended is None:
            recommended = conservative_price
        if recommended is not None and floor_price is not None:
            recommended = max(recommended, floor_price)
        market_cap_price = self._blend_prices(short_median, long_median, primary_ratio=0.65)
        if market_cap_price is None:
            market_cap_price = max(v for v in (fast_price, conservative_price, recommended) if v is not None)

        settle_seed = self._blend_prices(fast_settle, short_settle or conservative_settle, primary_ratio=0.7)
        settle = self._apply_trend_adjustment(settle_seed, trend_adjust_pct)
        if settle is None and recommended is not None:
            settle = self.calc_settle_price(recommended)

        exact_sample_count = sum(1 for r in pricing_records if getattr(r, "match_tier", "") == "exact_condition")
        fallback_sample_count = sum(
            1 for r in pricing_records
            if getattr(r, "match_tier", "") and getattr(r, "match_tier", "") != "exact_condition"
        )

        spread_ratio = self._spread_ratio(weighted)
        warning_parts = []
        if use_fallback_records:
            confidence = ConfidenceLevel.LOW
            warning_parts.append(f"近{self.near_days}天样本不足，已回退参考更早成交")
        else:
            confidence = ConfidenceLevel.HIGH
            warning_parts.append(f"近{self.near_days}天样本优先，近{short_days}天锚点权重更高")

        if trend_adjust_pct <= -0.5:
            warning_parts.append(f"近期行情走低，已下调 {abs(trend_adjust_pct):.1f}%")
        elif trend_adjust_pct >= 0.5:
            warning_parts.append(f"近期行情走高，已上调 {trend_adjust_pct:.1f}%")

        if spread_ratio >= 0.18:
            confidence = ConfidenceLevel.LOW
            warning_parts.append("价格波动较大，仅供参考")
        elif spread_ratio >= 0.12:
            warning_parts.append("价格有一定波动")

        if exact_sample_count and fallback_sample_count:
            warning_parts.append(f"精确样本 {exact_sample_count}，补充样本 {fallback_sample_count}")

        return PricingResult(
            title=title,
            sample_count=len(pricing_records),
            fast_price=fast_price,
            conservative_price=conservative_price,
            floor_price=floor_price,
            market_floor_price=market_floor_price,
            cost_floor_price=None,
            market_cap_price=market_cap_price,
            market_base_price=market_base_price,
            age_discount_factor=age_discount_factor,
            age_adjusted_price=age_adjusted_price,
            pre_rule_price=recommended,
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

            floor_guard = result.cost_floor_price or result.floor_price
            if floor_guard is not None and next_price < floor_guard:
                next_price = floor_guard
            if result.market_cap_price is not None and next_price > result.market_cap_price:
                next_price = result.market_cap_price

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
        return max(price * (1 - fee_rate) - station_fee, 0.0)

    @staticmethod
    def calc_price_from_settle_target(settle_target: Optional[float]) -> Optional[float]:
        if settle_target is None:
            return None
        fee_rate = getattr(cfg, "platform_fee_rate", PLATFORM_FEE_RATE)
        station_fee = getattr(cfg, "station_service_fee", STATION_SERVICE_FEE)
        net_rate = 1 - fee_rate
        if net_rate <= 0:
            return None
        return max((settle_target + station_fee) / net_rate, 0.0)

    def _record_weight(self, record: SoldRecord, now: datetime, allow_older: bool) -> int:
        days_ago = max((now - record.sold_time).total_seconds() / 86400, 0)
        base_weight = getattr(record, "match_weight", 1) or 1
        if allow_older:
            half_life_days = max(self.near_days / 2, 1)
        else:
            half_life_days = max(self.near_days / 3, 1)
        decay_weight = math.exp(-days_ago / half_life_days)
        return max(1, round(base_weight * 8 * decay_weight))

    def _build_weighted_series(self, records: List[SoldRecord], now: datetime, allow_older: bool, short_window: bool = False):
        weighted_prices = []
        weighted_settle = []
        for record in records:
            weight = self._record_weight(record, now, allow_older)
            if short_window:
                days_ago = max((now - record.sold_time).total_seconds() / 86400, 0)
                short_half_life = max(min(self.near_days / 4, 7), 1)
                short_decay = math.exp(-days_ago / short_half_life)
                weight = max(1, round(weight * (1 + short_decay)))
            weighted_prices.extend([record.sold_price] * weight)
            settle_price = record.settle_price
            if settle_price is None:
                settle_price = self.calc_settle_price(record.sold_price)
            if settle_price is not None:
                weighted_settle.extend([settle_price] * weight)
        return sorted(weighted_prices), sorted(weighted_settle)

    @staticmethod
    def _blend_prices(primary: Optional[float], secondary: Optional[float], primary_ratio: float = 0.7) -> Optional[float]:
        if primary is None:
            return secondary
        if secondary is None:
            return primary
        primary_ratio = max(0, min(primary_ratio, 1))
        return primary * primary_ratio + secondary * (1 - primary_ratio)

    @staticmethod
    def _trend_adjust_pct(short_price: Optional[float], long_price: Optional[float]) -> float:
        if short_price is None or long_price is None or long_price <= 0:
            return 0.0
        change_pct = ((short_price - long_price) / long_price) * 100
        return max(-8.0, min(change_pct * 0.35, 8.0))

    @staticmethod
    def _apply_trend_adjustment(price: Optional[float], adjust_pct: float) -> Optional[float]:
        if price is None:
            return None
        return price * (1 + adjust_pct / 100)

    @staticmethod
    def _spread_ratio(sorted_data) -> float:
        if not sorted_data:
            return 0.0
        median = PricingEngine._percentile(sorted_data, 50)
        if median in (None, 0):
            return 0.0
        p20 = PricingEngine._percentile(sorted_data, 20)
        p80 = PricingEngine._percentile(sorted_data, 80)
        if p20 is None or p80 is None:
            return 0.0
        return max(0.0, (p80 - p20) / median)

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
