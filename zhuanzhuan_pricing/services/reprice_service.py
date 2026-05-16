# -*- coding: utf-8 -*-
"""统一改价流水线服务"""
from __future__ import annotations

import copy
import datetime
import math
from typing import Any, Callable, Optional

from ..core.price_history import get_history_db

from ..config import cfg
from ..core.models import BatchItem
from ..core.pricing_engine import PricingEngine
from ..core.rule_engine import RuleEngine
from ..core.utils import round_to_8
from .data_store import SoldCache


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def get_custom_reprice_config() -> dict:
    mode = str(getattr(cfg, "auto_reprice_custom_offset_mode", "off") or "off").strip().lower()
    if mode not in {"off", "fixed", "percent"}:
        mode = "off"
    value = safe_float(getattr(cfg, "auto_reprice_custom_offset_value", 0.0), 0.0)
    return {
        "mode": mode,
        "value": value,
        "enabled": mode != "off" and abs(value) > 0,
    }


def describe_custom_reprice_offset(mode: str, value: float) -> str:
    if mode == "fixed":
        return f"固定微调 {value:+.0f} 元"
    if mode == "percent":
        return f"比例微调 {value:+.1f}%"
    return "关闭"


def apply_custom_reprice_offset(price: Optional[float], mode: str, value: float) -> Optional[float]:
    if price is None:
        return None
    if mode == "fixed":
        adjusted = price + value
    elif mode == "percent":
        adjusted = price * (1 + value / 100)
    else:
        adjusted = price
    return max(round(adjusted, 0), 0)


def min_target_profit(cost_price: float) -> float:
    if cost_price <= 0:
        return 0.0
    if cost_price < 1000:
        return 40.0
    if cost_price < 3000:
        return max(cost_price * 0.03, 60.0)
    return max(cost_price * 0.025, 100.0)


def cost_floor_price(cost_price: float) -> Optional[float]:
    if cost_price <= 0:
        return None
    settle_target = cost_price + min_target_profit(cost_price)
    target_price = PricingEngine.calc_price_from_settle_target(settle_target)
    if target_price is None:
        return None
    return round(target_price, 0)


def age_discount_factor(days_on_sale: int) -> float:
    days = max(int(days_on_sale or 0), 0)
    if days <= 3:
        return 1.0
    if days <= 15:
        return max(1.0 - (days - 3) * 0.003, 0.85)
    return max(1.0 - (12 * 0.003) - (days - 15) * 0.005, 0.7)


def clamp_price(price: Optional[float], floor_price: Optional[float], cap_price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    value = float(price)
    if floor_price is not None:
        value = max(value, floor_price)
    if cap_price is not None:
        value = min(value, cap_price)
    return round(value, 0)


def normalize_tail8_price(price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    floored = math.floor(float(price))
    if floored <= 0:
        return 0.0
    return float(round_to_8(floored))


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{float(value):.0f}"


def _fmt_signed_delta(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.0f}"


def _official_deviation_threshold_pct() -> float:
    value = safe_float(cfg.get("official_reference_deviation_pct", 10.0), 10.0)
    return max(value, 0.0)


def _conflict_guard_enabled() -> bool:
    return bool(cfg.get("manual_review_policy_enabled", False))


def _conflict_cooldown_minutes() -> int:
    return max(int(cfg.get("reprice_conflict_cooldown_minutes", 60) or 60), 0)


def _conflict_direction_lock_minutes() -> int:
    return max(int(cfg.get("reprice_direction_lock_minutes", 360) or 360), 0)


def _conflict_oscillation_window_hours() -> int:
    return max(int(cfg.get("reprice_oscillation_window_hours", 24) or 24), 1)


def _conflict_oscillation_min_flips() -> int:
    return max(int(cfg.get("reprice_oscillation_min_flips", 2) or 2), 1)


def _load_recent_price_changes(product_id: str, *, hours: int, limit: int = 20):
    if not product_id:
        return []
    db = get_history_db()
    recent = db.query(days=max(int(hours / 24) + 2, 2), limit=max(limit, 1))
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=max(hours, 1))
    rows = [r for r in recent if str(getattr(r, "product_id", "") or "") == str(product_id) and getattr(r, "timestamp", now) >= cutoff]
    rows.sort(key=lambda r: getattr(r, "timestamp", now))
    return rows


def _change_direction(diff: Optional[float]) -> int:
    if diff is None:
        return 0
    value = float(diff)
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _build_conflict_guard(item: Optional[BatchItem], final_price: Optional[float], *, enabled: bool) -> dict:
    guard = {
        "enabled": bool(enabled),
        "triggered": False,
        "reason": "",
        "reason_code": "",
        "cooldown_hit": False,
        "direction_lock_hit": False,
        "oscillation_hit": False,
        "recent_changes": 0,
        "oscillation_flips": 0,
    }
    if not enabled or item is None or final_price is None:
        return guard

    product_id = str(getattr(item, "product_id", "") or "")
    current_price = getattr(item, "current_price", None)
    if not product_id or current_price is None:
        return guard

    target_diff = float(final_price) - float(current_price)
    target_dir = _change_direction(target_diff)
    if target_dir == 0:
        return guard

    window_hours = _conflict_oscillation_window_hours()
    rows = _load_recent_price_changes(product_id, hours=window_hours, limit=40)
    guard["recent_changes"] = len(rows)
    if not rows:
        return guard

    now = datetime.datetime.now()
    last = rows[-1]
    last_ts = getattr(last, "timestamp", now)
    last_diff = getattr(last, "diff", None)
    last_dir = _change_direction(last_diff)

    cooldown_minutes = _conflict_cooldown_minutes()
    if cooldown_minutes > 0 and (now - last_ts).total_seconds() < cooldown_minutes * 60:
        guard.update({
            "triggered": True,
            "cooldown_hit": True,
            "reason_code": "cooldown",
            "reason": f"最近 {cooldown_minutes} 分钟内已改价，进入冷却期",
        })
        return guard

    direction_lock_minutes = _conflict_direction_lock_minutes()
    if direction_lock_minutes > 0 and last_dir != 0 and last_dir != target_dir and (now - last_ts).total_seconds() < direction_lock_minutes * 60:
        guard.update({
            "triggered": True,
            "direction_lock_hit": True,
            "reason_code": "direction_lock",
            "reason": f"最近改价方向相反，{direction_lock_minutes} 分钟内禁止反向拉回",
        })
        return guard

    flips = 0
    prev_dir = 0
    for row in rows:
        row_dir = _change_direction(getattr(row, "diff", None))
        if row_dir == 0:
            continue
        if prev_dir != 0 and row_dir != prev_dir:
            flips += 1
        prev_dir = row_dir
    guard["oscillation_flips"] = flips
    if flips >= _conflict_oscillation_min_flips():
        guard.update({
            "triggered": True,
            "oscillation_hit": True,
            "reason_code": "oscillation",
            "reason": f"近 {window_hours} 小时内改价方向反转 {flips} 次，疑似震荡",
        })
        return guard

    return guard


def _is_low_sample(pricing) -> bool:
    if pricing is None:
        return True
    sample_count = int(getattr(pricing, "sample_count", 0) or 0)
    min_sample = int(getattr(cfg, "engine_min_sample", 5) or 5)
    if sample_count < max(min_sample, 1):
        return True
    confidence = str(getattr(getattr(pricing, "confidence", None), "value", getattr(pricing, "confidence", "")) or "").strip().lower()
    warning = str(getattr(pricing, "warning", "") or "")
    return confidence == "low" and "样本不足" in warning


def _normalize_official_reference(official_reference: Optional[dict]) -> dict:
    data = official_reference or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "reference_price": safe_float(data.get("reference_price"), 0.0) if data.get("reference_price") not in (None, "") else None,
        "reference_settle_price": safe_float(data.get("reference_settle_price"), 0.0) if data.get("reference_settle_price") not in (None, "") else None,
        "grade_name": str(data.get("grade_name") or ""),
        "sku_id": str(data.get("sku_id") or ""),
    }


def _merge_manual_review_reason(base_reason: str, extra_reason: str) -> str:
    base = str(base_reason or "").strip()
    extra = str(extra_reason or "").strip()
    if not extra:
        return base
    if not base:
        return extra
    if extra in base:
        return base
    return f"{base}；{extra}"


def _apply_official_manual_review_guard(
    manual_review: dict,
    *,
    final_price: Optional[float],
    official_reference_price: Optional[float],
    low_sample: bool,
    official_used_as_anchor: bool,
) -> dict:
    updated = dict(manual_review or {})
    deviation_abs = None
    deviation_pct = None
    official_risk_triggered = False
    official_risk_reason = ""

    if final_price is not None and official_reference_price not in (None, 0):
        deviation_abs = round(float(final_price) - float(official_reference_price), 0)
        deviation_pct = round(abs(float(deviation_abs)) / float(official_reference_price) * 100, 2)
        if deviation_pct > _official_deviation_threshold_pct():
            official_risk_triggered = True
            official_risk_reason = (
                f"建议价与官方参考价偏离 {deviation_pct:.2f}%"
                f"（参考 {official_reference_price:.0f}）"
            )

    if low_sample and official_used_as_anchor:
        official_risk_triggered = True
        fallback_reason = "样本不足，已参考官方建议价，需人工确认"
        official_risk_reason = _merge_manual_review_reason(official_risk_reason, fallback_reason)

    if official_risk_triggered:
        updated["needs_manual_review"] = True
        updated["manual_review_reason"] = _merge_manual_review_reason(updated.get("manual_review_reason", ""), official_risk_reason)

    updated["official_deviation_abs"] = deviation_abs
    updated["official_deviation_pct"] = deviation_pct
    updated["official_risk_triggered"] = official_risk_triggered
    updated["official_risk_reason"] = official_risk_reason
    return updated


def _pick_pricing_anchor_price(pricing, current_price: Optional[float]) -> Optional[float]:
    if pricing is None:
        return None
    market_base = getattr(pricing, "market_base_price", None)
    fast_price = getattr(pricing, "fast_price", None)
    conservative_price = getattr(pricing, "conservative_price", None)
    if market_base is not None and current_price is not None:
        drift = abs(float(current_price) - float(market_base))
        if drift >= 150:
            near_current = PricingEngine._blend_prices(current_price, market_base, primary_ratio=0.35)
            if near_current is not None:
                return round(near_current, 0)
    if market_base is not None:
        return round(float(market_base), 0)
    blended = PricingEngine._blend_prices(fast_price, conservative_price, primary_ratio=0.55)
    if blended is not None:
        return round(blended, 0)
    suggested = pricing.suggest_price()
    return round(float(suggested), 0) if suggested is not None else None


def _default_turnover_segment_profiles() -> dict[str, dict[str, float]]:
    return {
        "default": {
            "anchor_to_current_ratio": 0.0,
            "age_discount_multiplier": 1.0,
            "market_cap_multiplier": 1.0,
        },
        "fast_turnover": {
            "anchor_to_current_ratio": 0.5,
            "age_discount_multiplier": 1.08,
            "market_cap_multiplier": 0.995,
        },
        "balanced": {
            "anchor_to_current_ratio": 0.25,
            "age_discount_multiplier": 1.0,
            "market_cap_multiplier": 1.0,
        },
        "slow_turnover": {
            "anchor_to_current_ratio": 0.15,
            "age_discount_multiplier": 0.92,
            "market_cap_multiplier": 0.98,
        },
    }


def _load_turnover_segment_profiles() -> dict[str, dict[str, float]]:
    raw = cfg.get("turnover_segment_profiles", {})
    defaults = _default_turnover_segment_profiles()
    if not isinstance(raw, dict):
        return defaults
    merged = dict(defaults)
    for name, profile in raw.items():
        if not isinstance(profile, dict):
            continue
        base = dict(defaults.get(name, defaults["default"]))
        for key in ("anchor_to_current_ratio", "age_discount_multiplier", "market_cap_multiplier"):
            if key in profile:
                base[key] = safe_float(profile.get(key), base.get(key, 0.0))
        merged[str(name)] = base
    return merged


def _resolve_turnover_segment(item: Optional[BatchItem], pricing, *, low_sample: bool) -> str:
    if item is None:
        return "default"
    days_on_sale = max(int(getattr(item, "days_on_sale", 0) or 0), 0)
    cost_price = safe_float(getattr(item, "cost_price", 0.0), 0.0)
    sample_count = int(getattr(pricing, "sample_count", 0) or 0) if pricing is not None else 0

    if low_sample or sample_count < 5 or days_on_sale >= 21:
        return "slow_turnover"
    if cost_price > 0 and cost_price <= 1500 and days_on_sale <= 10:
        return "fast_turnover"
    return "balanced"


def _is_turnover_segment_profile_enabled() -> bool:
    return bool(cfg.get("turnover_segment_profile_enabled", False))


def _apply_turnover_segment_profile(
    profile: dict[str, float],
    *,
    current_price: Optional[float],
    pricing_anchor_price: Optional[float],
    market_cap: Optional[float],
    current_age_discount: float,
) -> tuple[Optional[float], Optional[float], float]:
    anchor_ratio = max(0.0, min(safe_float(profile.get("anchor_to_current_ratio", 0.0), 0.0), 1.0))
    age_multiplier = max(0.7, min(safe_float(profile.get("age_discount_multiplier", 1.0), 1.0), 1.2))
    cap_multiplier = max(0.9, min(safe_float(profile.get("market_cap_multiplier", 1.0), 1.0), 1.05))

    next_anchor = pricing_anchor_price
    if current_price is not None and pricing_anchor_price is not None and anchor_ratio > 0:
        blended = PricingEngine._blend_prices(current_price, pricing_anchor_price, primary_ratio=anchor_ratio)
        if blended is not None:
            next_anchor = round(float(blended), 0)

    next_market_cap = market_cap
    if market_cap is not None:
        next_market_cap = round(float(market_cap) * cap_multiplier, 0)

    next_age_discount = max(0.6, min(current_age_discount * age_multiplier, 1.15))
    return next_anchor, next_market_cap, next_age_discount


def _build_manual_review_summary(decision: dict) -> str:
    if not decision.get("needs_manual_review"):
        return ""
    parts = ["需人工确认"]
    if decision.get("final_price") is not None:
        parts.append(f"建议价 {decision['final_price']:.0f}")
    if decision.get("final_settle_price") is not None:
        parts.append(f"预计结算价 {decision['final_settle_price']:.0f}")
    if decision.get("loss_amount"):
        parts.append(f"预计亏损 {decision['loss_amount']:.0f}")
    elif decision.get("target_profit_gap"):
        parts.append(f"利润缺口 {decision['target_profit_gap']:.0f}")
    if decision.get("manual_review_reason"):
        parts.append(str(decision["manual_review_reason"]))
    return "｜".join(parts)


def _build_profit_summary(decision: dict) -> str:
    if decision.get("needs_manual_review"):
        return _build_manual_review_summary(decision)
    if decision.get("below_target_profit") and decision.get("target_profit_gap"):
        return f"低于目标利润 {decision['target_profit_gap']:.0f}"
    return ""


def _build_decision_steps(
    *,
    market_base_price: Optional[float],
    fast_price: Optional[float],
    conservative_price: Optional[float],
    pricing_anchor_price: Optional[float],
    age_adjusted_price: Optional[float],
    current_age_discount: float,
    market_floor: Optional[float],
    market_cap: Optional[float],
    base_candidate_price: Optional[float],
    rule_adjusted_price: Optional[float],
    final_price: Optional[float],
    current_price: Optional[float],
    custom_enabled: bool,
    custom_summary: str,
    manual_review_reason: str,
    below_target_profit: bool,
    target_profit_gap: float,
) -> list[str]:
    steps: list[str] = []
    if market_base_price is not None or fast_price is not None or conservative_price is not None:
        steps.append(
            "市场信号 "
            f"基准 {_fmt_price(market_base_price)} / 极速 {_fmt_price(fast_price)} / 保守 {_fmt_price(conservative_price)}"
        )
    if pricing_anchor_price is not None:
        steps.append(f"建议锚点取 {_fmt_price(pricing_anchor_price)}")
    if age_adjusted_price is not None and current_age_discount != 1.0:
        steps.append(f"按库龄系数 {current_age_discount:.3f} 调整到 {_fmt_price(age_adjusted_price)}")
    if current_price is not None and final_price is not None:
        price_delta = round(final_price - current_price, 0)
        if abs(price_delta) < 1:
            steps.append(f"相对当前价 {_fmt_price(current_price)} 基本无调整空间")
        else:
            steps.append(f"相对当前价 {_fmt_price(current_price)} 调整 {_fmt_signed_delta(price_delta)}")
    if market_floor is not None or market_cap is not None:
        steps.append(
            f"候选价落在市场保护区间 {_fmt_price(market_floor)} ~ {_fmt_price(market_cap)}，得到 {_fmt_price(base_candidate_price)}"
        )
    elif base_candidate_price is not None:
        steps.append(f"规则前候选价 {_fmt_price(base_candidate_price)}")
    if rule_adjusted_price is not None:
        steps.append(f"规则后系统建议价 {_fmt_price(rule_adjusted_price)}")
    if custom_enabled and final_price is not None:
        steps.append(f"叠加价格微调 {custom_summary}，最终价 {_fmt_price(final_price)}")
    elif final_price is not None:
        steps.append(f"最终执行价 {_fmt_price(final_price)}")
    if manual_review_reason:
        steps.append(f"人工确认：{manual_review_reason}")
    elif below_target_profit and target_profit_gap > 0:
        steps.append(f"利润提醒：低于目标利润 {_fmt_price(target_profit_gap)}")
    return steps


def _build_decision_summary(decision: dict) -> str:
    final_price = decision.get("final_price")
    final_settle = decision.get("final_settle_price")
    current_price = decision.get("current_price")
    parts = []
    if current_price is not None or final_price is not None:
        parts.append(f"当前 {_fmt_price(current_price)} -> 最终 {_fmt_price(final_price)}")
    if final_settle is not None:
        parts.append(f"预计结算价 {_fmt_price(final_settle)}")
    profit_summary = _build_profit_summary(decision)
    if profit_summary:
        parts.append(profit_summary)
    return "｜".join(parts)


def _build_decision_explain_lines(
    pricing,
    *,
    market_base_price: Optional[float],
    fast_price: Optional[float],
    conservative_price: Optional[float],
    current_price: Optional[float],
    pricing_anchor_price: Optional[float],
    age_adjusted_price: Optional[float],
    current_age_discount: float,
    base_candidate_price: Optional[float],
    rule_adjusted_price: Optional[float],
    final_price: Optional[float],
    custom_enabled: bool,
    custom_summary: str,
    market_floor: Optional[float],
    market_cap: Optional[float],
    below_target_profit: bool,
    target_profit_gap: float,
    needs_manual_review: bool,
    loss_amount: float,
    manual_review_reason: str,
    extra_steps: Optional[list[str]] = None,
    rule_engine: Optional[RuleEngine] = None,
    item: Optional[BatchItem] = None,
    working_pricing=None,
) -> list[str]:
    explain_lines: list[str] = [step for step in (extra_steps or []) if step]
    if rule_adjusted_price is not None and getattr(working_pricing, "rule_hit", ""):
        explain_lines.append(f"规则命中 {working_pricing.rule_hit}")
    elif rule_adjusted_price is not None:
        explain_lines.append("规则未命中，沿用基础建议价")
    if custom_enabled and final_price is not None:
        explain_lines.append(f"价格微调 {custom_summary}")
    if needs_manual_review:
        explain_lines.append(f"人工确认 预计亏损 {_fmt_price(loss_amount)} | {manual_review_reason}")
    elif below_target_profit and target_profit_gap > 0:
        explain_lines.append(f"利润提醒 低于目标利润 {_fmt_price(target_profit_gap)}")
    if rule_engine is not None and item is not None and working_pricing is not None:
        explain_lines.extend([line for line in rule_engine.explain(item, working_pricing) if line])
    if pricing is not None and getattr(pricing, "warning", ""):
        explain_lines.append(f"样本提示 {pricing.warning}")
    return explain_lines


def assess_manual_review(item: Optional[BatchItem], price: Optional[float], cost_floor: Optional[float]) -> dict:
    settle_price = PricingEngine.calc_settle_price(price) if price is not None else None
    cost_price = safe_float(getattr(item, "cost_price", 0.0) if item is not None else 0.0, 0.0)
    min_profit = min_target_profit(cost_price) if cost_price > 0 else 0.0
    target_settle = cost_price + min_profit if cost_price > 0 else None
    below_cost = bool(cost_price > 0 and settle_price is not None and settle_price < cost_price)
    below_target_profit = bool(target_settle is not None and settle_price is not None and settle_price < target_settle)
    needs_manual_review = bool(below_cost or below_target_profit)
    loss_amount = round(cost_price - settle_price, 0) if below_cost and settle_price is not None else 0.0
    target_profit_gap = round(target_settle - settle_price, 0) if below_target_profit and target_settle is not None and settle_price is not None else 0.0
    price_gap_to_cost_floor = round(cost_floor - price, 0) if cost_floor is not None and price is not None and price < cost_floor else 0.0

    reason = ""
    if below_cost and settle_price is not None:
        reason = f"建议价预计结算价 {settle_price:.0f} 低于成本 {cost_price:.0f}，需人工确认"
    elif below_target_profit and settle_price is not None and target_settle is not None:
        reason = f"建议价预计结算价 {settle_price:.0f} 低于目标结算价 {target_settle:.0f}，需人工确认"

    return {
        "settle_price": settle_price,
        "settled_price": settle_price,
        "cost_price": cost_price,
        "target_settle": target_settle,
        "target_settled": target_settle,
        "below_cost": below_cost,
        "below_target_profit": below_target_profit,
        "needs_manual_review": needs_manual_review,
        "loss_amount": loss_amount,
        "target_profit_gap": target_profit_gap,
        "price_gap_to_cost_floor": price_gap_to_cost_floor,
        "manual_review_reason": reason,
    }


def build_reprice_decision(
    item: Optional[BatchItem],
    pricing,
    rule_engine: Optional[RuleEngine] = None,
    *,
    custom_mode: Optional[str] = None,
    custom_value: Optional[float] = None,
    apply_rules: bool = False,
    official_reference: Optional[dict] = None,
) -> dict:
    config = get_custom_reprice_config()
    mode = str(custom_mode if custom_mode is not None else config["mode"] or "off").strip().lower()
    if mode not in {"off", "fixed", "percent"}:
        mode = "off"
    value = safe_float(custom_value if custom_value is not None else config["value"], 0.0)
    custom_enabled = mode != "off" and abs(value) > 0

    official = _normalize_official_reference(official_reference)
    official_reference_price = official["reference_price"]
    official_reference_settle_price = official["reference_settle_price"]
    official_grade_name = official["grade_name"]
    official_sku_id = official["sku_id"]
    low_sample = _is_low_sample(pricing)
    official_reference_used_as_anchor = False

    system_price = None
    market_floor = None
    market_cap = None
    cost_floor = None
    current_age_discount = 1.0
    age_adjusted_price = None
    market_base_price = None
    pricing_anchor_price = None
    base_candidate_price = None
    current_price = safe_float(getattr(item, "current_price", None), 0.0) if item is not None and getattr(item, "current_price", None) is not None else None
    turnover_segment = "default"
    turnover_profile = _default_turnover_segment_profiles()["default"]
    working_pricing = copy.deepcopy(pricing) if pricing is not None else None
    if working_pricing is not None:
        market_base_price = working_pricing.market_base_price
        market_cap = working_pricing.market_cap_price
        market_floor = working_pricing.market_floor_price or working_pricing.floor_price
        pricing_anchor_price = _pick_pricing_anchor_price(working_pricing, current_price)
        if item is not None:
            current_age_discount = age_discount_factor(item.days_on_sale)
            cost_floor = cost_floor_price(float(getattr(item, "cost_price", 0.0) or 0.0))
            turnover_segment = _resolve_turnover_segment(item, working_pricing, low_sample=low_sample)
            profiles = _load_turnover_segment_profiles()
            turnover_profile = profiles.get(turnover_segment, profiles.get("default", _default_turnover_segment_profiles()["default"]))
            if _is_turnover_segment_profile_enabled():
                pricing_anchor_price, market_cap, current_age_discount = _apply_turnover_segment_profile(
                    turnover_profile,
                    current_price=current_price,
                    pricing_anchor_price=pricing_anchor_price,
                    market_cap=market_cap,
                    current_age_discount=current_age_discount,
                )
            if pricing_anchor_price is not None:
                age_adjusted_price = round(pricing_anchor_price * current_age_discount, 0)
        working_pricing.cost_floor_price = cost_floor
        working_pricing.age_discount_factor = current_age_discount
        working_pricing.age_adjusted_price = age_adjusted_price
        if market_base_price is not None:
            working_pricing.market_base_price = market_base_price
        base_candidate_price = age_adjusted_price if age_adjusted_price is not None else pricing_anchor_price
        base_candidate_price = clamp_price(base_candidate_price, market_floor, market_cap)
        working_pricing.pre_rule_price = base_candidate_price
        working_pricing.recommended_price = base_candidate_price
        working_pricing.settle_price = PricingEngine.calc_settle_price(base_candidate_price) if base_candidate_price is not None else None
        if apply_rules and rule_engine is not None and item is not None:
            system_price = rule_engine.apply(item, working_pricing) or working_pricing.suggest_price()
        else:
            system_price = working_pricing.suggest_price()
    system_price = clamp_price(system_price, market_floor, market_cap)
    if system_price is None and low_sample and official_reference_price is not None:
        system_price = clamp_price(official_reference_price, market_floor, market_cap)
        official_reference_used_as_anchor = system_price is not None
    if working_pricing is not None:
        working_pricing.recommended_price = system_price
        working_pricing.settle_price = PricingEngine.calc_settle_price(system_price) if system_price is not None else None

    final_price = apply_custom_reprice_offset(system_price, mode, value)
    final_price = clamp_price(final_price, market_floor, market_cap)
    final_price = normalize_tail8_price(final_price)
    manual_review = assess_manual_review(item, final_price, cost_floor)
    manual_review = _apply_official_manual_review_guard(
        manual_review,
        final_price=final_price,
        official_reference_price=official_reference_price,
        low_sample=low_sample,
        official_used_as_anchor=official_reference_used_as_anchor,
    )
    conflict_guard = _build_conflict_guard(item, final_price, enabled=_conflict_guard_enabled())
    if conflict_guard.get("triggered"):
        manual_review["needs_manual_review"] = True
        manual_review["manual_review_reason"] = _merge_manual_review_reason(
            manual_review.get("manual_review_reason", ""),
            str(conflict_guard.get("reason") or "策略冲突，需人工确认"),
        )
    risk_sources: list[str] = []
    if manual_review.get("below_cost"):
        risk_sources.append("below_cost")
    if manual_review.get("below_target_profit"):
        risk_sources.append("below_target_profit")
    if manual_review.get("official_risk_triggered"):
        risk_sources.append("official_deviation")
    if low_sample:
        risk_sources.append("low_sample")
    if conflict_guard.get("cooldown_hit"):
        risk_sources.append("cooldown")
    if conflict_guard.get("direction_lock_hit"):
        risk_sources.append("direction_lock")
    if conflict_guard.get("oscillation_hit"):
        risk_sources.append("oscillation")
    if not risk_sources:
        risk_sources.append("none")
    if "below_cost" in risk_sources or "oscillation" in risk_sources:
        risk_level = "high"
    elif any(tag in risk_sources for tag in ("below_target_profit", "official_deviation", "direction_lock", "cooldown")):
        risk_level = "medium"
    else:
        risk_level = "low"
    risk_source_primary = risk_sources[0]
    price_delta = round(final_price - current_price, 0) if final_price is not None and current_price is not None else None

    decision_steps = _build_decision_steps(
        market_base_price=market_base_price,
        fast_price=getattr(working_pricing, "fast_price", None) if working_pricing is not None else None,
        conservative_price=getattr(working_pricing, "conservative_price", None) if working_pricing is not None else None,
        pricing_anchor_price=pricing_anchor_price,
        age_adjusted_price=age_adjusted_price,
        current_age_discount=current_age_discount,
        market_floor=market_floor,
        market_cap=market_cap,
        base_candidate_price=base_candidate_price,
        rule_adjusted_price=system_price,
        final_price=final_price,
        current_price=current_price,
        custom_enabled=custom_enabled,
        custom_summary=describe_custom_reprice_offset(mode, value),
        manual_review_reason=manual_review["manual_review_reason"],
        below_target_profit=manual_review["below_target_profit"],
        target_profit_gap=manual_review["target_profit_gap"],
    )
    explain_lines = _build_decision_explain_lines(
        pricing,
        market_base_price=market_base_price,
        fast_price=getattr(working_pricing, "fast_price", None) if working_pricing is not None else None,
        conservative_price=getattr(working_pricing, "conservative_price", None) if working_pricing is not None else None,
        current_price=current_price,
        pricing_anchor_price=pricing_anchor_price,
        age_adjusted_price=age_adjusted_price,
        current_age_discount=current_age_discount,
        base_candidate_price=base_candidate_price,
        rule_adjusted_price=system_price,
        final_price=final_price,
        custom_enabled=custom_enabled,
        custom_summary=describe_custom_reprice_offset(mode, value),
        market_floor=market_floor,
        market_cap=market_cap,
        below_target_profit=manual_review["below_target_profit"],
        target_profit_gap=manual_review["target_profit_gap"],
        needs_manual_review=manual_review["needs_manual_review"],
        loss_amount=manual_review["loss_amount"],
        manual_review_reason=manual_review["manual_review_reason"],
        extra_steps=decision_steps,
        rule_engine=rule_engine,
        item=item,
        working_pricing=working_pricing,
    )

    return {
        "pricing": working_pricing,
        "turnover_segment": turnover_segment,
        "turnover_profile": turnover_profile,
        "system_price": system_price,
        "system_settle_price": PricingEngine.calc_settle_price(system_price) if system_price is not None else None,
        "system_settled_price": PricingEngine.calc_settle_price(system_price) if system_price is not None else None,
        "final_price": final_price,
        "final_settle_price": manual_review["settle_price"],
        "final_settled_price": manual_review["settle_price"],
        "custom_mode": mode,
        "custom_value": value,
        "custom_enabled": custom_enabled,
        "custom_summary": describe_custom_reprice_offset(mode, value),
        "rule_hit": getattr(working_pricing, "rule_hit", "") if working_pricing is not None else "",
        "floor_guard": market_floor,
        "market_floor": market_floor,
        "market_cap": market_cap,
        "cost_floor": cost_floor,
        "age_discount_factor": current_age_discount,
        "age_adjusted_price": age_adjusted_price,
        "market_base_price": market_base_price,
        "pricing_anchor_price": pricing_anchor_price,
        "current_price": current_price,
        "price_delta": price_delta,
        "base_candidate_price": base_candidate_price,
        "rule_adjusted_price": system_price,
        "below_cost": manual_review["below_cost"],
        "below_target_profit": manual_review["below_target_profit"],
        "needs_manual_review": manual_review["needs_manual_review"],
        "manual_review_reason": manual_review["manual_review_reason"],
        "loss_amount": manual_review["loss_amount"],
        "target_profit_gap": manual_review["target_profit_gap"],
        "price_gap_to_cost_floor": manual_review["price_gap_to_cost_floor"],
        "target_settle": manual_review["target_settle"],
        "target_settled": manual_review["target_settle"],
        "cost_price": manual_review["cost_price"],
        "decision_flags": {
            "below_cost": manual_review["below_cost"],
            "below_target_profit": manual_review["below_target_profit"],
            "needs_manual_review": manual_review["needs_manual_review"],
        },
        "decision_steps": decision_steps,
        "explain_lines": explain_lines,
        "official_reference_price": official_reference_price,
        "official_reference_settle_price": official_reference_settle_price,
        "official_reference_grade_name": official_grade_name,
        "official_reference_sku_id": official_sku_id,
        "official_reference_used_as_anchor": official_reference_used_as_anchor,
        "official_deviation_abs": manual_review.get("official_deviation_abs"),
        "official_deviation_pct": manual_review.get("official_deviation_pct"),
        "official_risk_triggered": manual_review.get("official_risk_triggered", False),
        "official_risk_reason": manual_review.get("official_risk_reason", ""),
        "low_sample": low_sample,
        "risk_source_primary": risk_source_primary,
        "risk_sources": risk_sources,
        "risk_level": risk_level,
        "conflict_guard": conflict_guard,
    }


def pricing_preview(pricing, rule_engine: Optional[RuleEngine] = None, item: Optional[BatchItem] = None, decision: Optional[dict] = None) -> str:
    if pricing is None:
        return "无定价结果"
    if decision is None:
        decision = build_reprice_decision(item, pricing, rule_engine, apply_rules=True)
    current_price = decision.get("current_price")
    price_delta = decision.get("price_delta")
    parts = [
        f"样本 {pricing.sample_count}",
        f"置信度 {getattr(pricing.confidence, 'value', pricing.confidence) or '-'}",
        _build_decision_summary(decision) or "当前 - -> 最终 -",
    ]
    if decision.get("system_price") is not None:
        parts.append(f"系统建议价 {decision['system_price']:.0f}")
    if price_delta is not None:
        parts.append(f"相对当前 {_fmt_signed_delta(price_delta)}")
    if decision.get("market_base_price") is not None:
        parts.append(f"市场基准 {decision['market_base_price']:.0f}")
    if decision.get("pricing_anchor_price") is not None:
        parts.append(f"建议锚点 {decision['pricing_anchor_price']:.0f}")
    if decision.get("age_adjusted_price") is not None:
        parts.append(f"库龄后 {decision['age_adjusted_price']:.0f}")
    if decision.get("cost_floor") is not None:
        parts.append(f"成本底线 {decision['cost_floor']:.0f}")
    if pricing.market_floor_price is not None:
        parts.append(f"市场底 {pricing.market_floor_price:.0f}")
    elif pricing.floor_price is not None:
        parts.append(f"底价 {pricing.floor_price:.0f}")
    if decision.get("official_reference_price") is not None:
        parts.append(f"官方参考价 {decision['official_reference_price']:.0f}")
    if decision.get("official_deviation_pct") is not None:
        parts.append(f"官方偏离 {decision['official_deviation_pct']:.2f}%")
    if decision.get("official_risk_triggered"):
        parts.append("官方风控 已触发")
    parts.append(f"自定义 {decision['custom_summary']}")
    if pricing.rule_hit:
        parts.append(f"规则 {pricing.rule_hit}")
    if pricing.warning:
        parts.append(f"提示 {pricing.warning}")
    decision_steps = decision.get("decision_steps") or []
    if decision_steps:
        parts.append("决策链路 " + " | ".join(decision_steps[:4]))
    explain_lines = decision.get("explain_lines") or []
    if explain_lines:
        parts.append("说明 " + " | ".join(explain_lines[:3]))
    return "；".join(parts)
def recalc_decision_with_final_price(
    item: Optional[BatchItem],
    decision: dict,
    final_price: Optional[float],
    *,
    extra_steps: Optional[list[str]] = None,
) -> dict:
    next_price = clamp_price(final_price, decision.get("market_floor"), decision.get("market_cap"))
    next_price = normalize_tail8_price(next_price)
    manual_review = assess_manual_review(item, next_price, decision.get("cost_floor"))
    manual_review = _apply_official_manual_review_guard(
        manual_review,
        final_price=next_price,
        official_reference_price=decision.get("official_reference_price"),
        low_sample=bool(decision.get("low_sample", False)),
        official_used_as_anchor=bool(decision.get("official_reference_used_as_anchor", False)),
    )
    conflict_guard = _build_conflict_guard(item, next_price, enabled=_conflict_guard_enabled())
    if conflict_guard.get("triggered"):
        manual_review["needs_manual_review"] = True
        manual_review["manual_review_reason"] = _merge_manual_review_reason(
            manual_review.get("manual_review_reason", ""),
            str(conflict_guard.get("reason") or "策略冲突，需人工确认"),
        )
    risk_sources: list[str] = []
    if manual_review.get("below_cost"):
        risk_sources.append("below_cost")
    if manual_review.get("below_target_profit"):
        risk_sources.append("below_target_profit")
    if manual_review.get("official_risk_triggered"):
        risk_sources.append("official_deviation")
    if bool(decision.get("low_sample", False)):
        risk_sources.append("low_sample")
    if conflict_guard.get("cooldown_hit"):
        risk_sources.append("cooldown")
    if conflict_guard.get("direction_lock_hit"):
        risk_sources.append("direction_lock")
    if conflict_guard.get("oscillation_hit"):
        risk_sources.append("oscillation")
    if not risk_sources:
        risk_sources.append("none")
    if "below_cost" in risk_sources or "oscillation" in risk_sources:
        risk_level = "high"
    elif any(tag in risk_sources for tag in ("below_target_profit", "official_deviation", "direction_lock", "cooldown")):
        risk_level = "medium"
    else:
        risk_level = "low"
    risk_source_primary = risk_sources[0]

    current_price = decision.get("current_price")
    price_delta = round(next_price - current_price, 0) if next_price is not None and current_price is not None else None
    prior_steps = list(decision.get("decision_steps") or [])
    stage_steps = [step for step in (extra_steps or []) if step]
    decision_steps = prior_steps + stage_steps
    explain_lines = _build_decision_explain_lines(
        decision.get("pricing"),
        market_base_price=decision.get("market_base_price"),
        fast_price=getattr(decision.get("pricing"), "fast_price", None) if decision.get("pricing") is not None else None,
        conservative_price=getattr(decision.get("pricing"), "conservative_price", None) if decision.get("pricing") is not None else None,
        current_price=current_price,
        pricing_anchor_price=decision.get("pricing_anchor_price"),
        age_adjusted_price=decision.get("age_adjusted_price"),
        current_age_discount=decision.get("age_discount_factor") or 1.0,
        base_candidate_price=decision.get("base_candidate_price"),
        rule_adjusted_price=decision.get("rule_adjusted_price"),
        final_price=next_price,
        custom_enabled=decision.get("custom_enabled", False),
        custom_summary=decision.get("custom_summary", "关闭"),
        market_floor=decision.get("market_floor"),
        market_cap=decision.get("market_cap"),
        below_target_profit=manual_review["below_target_profit"],
        target_profit_gap=manual_review["target_profit_gap"],
        needs_manual_review=manual_review["needs_manual_review"],
        loss_amount=manual_review["loss_amount"],
        manual_review_reason=manual_review["manual_review_reason"],
        extra_steps=decision_steps,
    )
    return {
        **decision,
        "final_price": next_price,
        "final_settle_price": manual_review.get("settle_price"),
        "final_settled_price": manual_review.get("settle_price"),
        "below_cost": manual_review["below_cost"],
        "below_target_profit": manual_review["below_target_profit"],
        "needs_manual_review": manual_review["needs_manual_review"],
        "manual_review_reason": manual_review["manual_review_reason"],
        "loss_amount": manual_review["loss_amount"],
        "target_profit_gap": manual_review["target_profit_gap"],
        "price_gap_to_cost_floor": manual_review["price_gap_to_cost_floor"],
        "target_settle": manual_review["target_settle"],
        "target_settled": manual_review["target_settle"],
        "cost_price": manual_review["cost_price"],
        "official_deviation_abs": manual_review.get("official_deviation_abs"),
        "official_deviation_pct": manual_review.get("official_deviation_pct"),
        "official_risk_triggered": manual_review.get("official_risk_triggered", False),
        "official_risk_reason": manual_review.get("official_risk_reason", ""),
        "decision_flags": {
            "below_cost": manual_review["below_cost"],
            "below_target_profit": manual_review["below_target_profit"],
            "needs_manual_review": manual_review["needs_manual_review"],
        },
        "decision_steps": decision_steps,
        "explain_lines": explain_lines,
        "risk_source_primary": risk_source_primary,
        "risk_sources": risk_sources,
        "risk_level": risk_level,
        "conflict_guard": conflict_guard,
    }


def run_reprice_pipeline(
    item: BatchItem,
    sold_cache: SoldCache,
    rule_engine: Optional[RuleEngine] = None,
    *,
    custom_mode: Optional[str] = None,
    custom_value: Optional[float] = None,
    apply_rules: bool = True,
    pricing_engine: Optional[PricingEngine] = None,
    settle_price_estimator=None,
    official_reference_fetcher: Optional[Callable[[BatchItem], Optional[dict]]] = None,
) -> dict:
    engine = pricing_engine or PricingEngine()
    records = sold_cache.filter_by_model_key(item.model, item.condition, item.capacity, item.color)
    pricing = engine.calculate(records, item.model, item.condition, item.capacity, item.color)
    official_reference = None
    if official_reference_fetcher is not None:
        try:
            official_reference = official_reference_fetcher(item)
        except Exception:
            official_reference = None
    decision = build_reprice_decision(
        item,
        pricing,
        rule_engine,
        custom_mode=custom_mode,
        custom_value=custom_value,
        apply_rules=apply_rules,
        official_reference=official_reference,
    )
    suggested_price = decision.get("final_price")
    suggested_settle_price = decision.get("final_settle_price")
    suggested_settled_price = decision.get("final_settled_price")
    if settle_price_estimator is not None and suggested_price is not None:
        estimated = settle_price_estimator(float(suggested_price))
        if estimated is not None:
            suggested_settle_price = estimated
            suggested_settled_price = estimated
    preview = pricing_preview(pricing, rule_engine, item, decision)
    return {
        "pricing": pricing,
        "decision": decision,
        "preview": preview,
        "suggested_price": suggested_price,
        "suggested_settle_price": suggested_settle_price,
        "suggested_settled_price": suggested_settled_price,
    }
