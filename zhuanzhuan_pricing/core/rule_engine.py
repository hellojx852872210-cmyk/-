# -*- coding: utf-8 -*-
"""规则引擎"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import RULE_CONFIG_FILE, ensure_parent_dir
from .models import BatchItem, PricingResult, PricingRule, RuleActionType


class RuleEngine:
    def __init__(self, rule_file=None):
        self._file = Path(rule_file or RULE_CONFIG_FILE)
        self._rules = []
        self.load()

    def load(self):
        if not self._file.exists():
            self._rules = self._default_rules()
            self.save()
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._rules = [PricingRule.from_dict(d) for d in data.get("rules", [])]
        except Exception:
            self._rules = []

    def save(self):
        data = {"rules": [r.to_dict() for r in self._rules]}
        target = ensure_parent_dir(self._file)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @property
    def rules(self):
        return list(self._rules)

    def add_rule(self, rule):
        self._rules.append(rule)
        self.save()

    def update_rule(self, index, rule):
        self._rules[index] = rule
        self.save()

    def delete_rule(self, index):
        self._rules.pop(index)
        self.save()

    def move_rule(self, index, direction):
        new_idx = index + direction
        if 0 <= new_idx < len(self._rules):
            self._rules[index], self._rules[new_idx] = self._rules[new_idx], self._rules[index]
            self.save()

    def get_enabled_rules(self):
        return [r for r in self._rules if r.enabled]

    def apply(self, item: BatchItem, pricing: PricingResult):
        from .pricing_engine import PricingEngine

        updated = PricingEngine().apply_rules(item, pricing, self.get_enabled_rules())
        return updated.recommended_price

    def explain(self, item: BatchItem, pricing: PricingResult):
        lines = []
        for rule in sorted(self.get_enabled_rules(), key=lambda r: r.priority):
            matched = rule.match.matches(item)
            prefix = "✓" if matched else "·"
            lines.append(f"{prefix} P{rule.priority} {rule.name} — {self._describe_action(rule.action.type, rule.action.adjust_pct, rule.action.amount)}")
        if pricing.rule_hit:
            lines.append(f"命中规则：{pricing.rule_hit}")
        elif not lines:
            lines.append("无启用规则")
        else:
            lines.append("未命中任何规则，使用基础建议价")
        return lines

    @staticmethod
    def _describe_action(action_type: RuleActionType, adjust_pct: float, amount: float) -> str:
        if action_type == RuleActionType.FAST_PRICE:
            return f"极速价 {adjust_pct:+.0f}%"
        if action_type == RuleActionType.CONSERVATIVE_PRICE:
            return f"保守价 {adjust_pct:+.0f}%"
        if action_type == RuleActionType.FIXED_DROP:
            return f"直降 {amount:.0f}"
        if action_type == RuleActionType.PCT_DROP:
            return f"降幅 {adjust_pct:.0f}%"
        if action_type == RuleActionType.FLOOR_PRICE:
            return "取底价"
        return action_type.value

    @staticmethod
    def _default_rules():
        defaults = [
            {"name": "iPhone 极速动销", "priority": 10, "enabled": True,
             "match": {"model_contains": "iPhone"}, "action": {"type": "fast_price", "adjust_pct": 0}},
            {"name": "华为旗舰保守定价", "priority": 20, "enabled": True,
             "match": {"model_contains": "Mate"}, "action": {"type": "conservative_price", "adjust_pct": -2}},
            {"name": "滞销7天降阶", "priority": 50, "enabled": True,
             "match": {"stale_days_gte": 7, "stale_days_lt": 14}, "action": {"type": "fixed_drop", "amount": 100}},
            {"name": "滞销14天深降", "priority": 51, "enabled": True,
             "match": {"stale_days_gte": 14}, "action": {"type": "pct_drop", "adjust_pct": 5}},
        ]
        return [PricingRule.from_dict(d) for d in defaults]
