# -*- coding: utf-8 -*-
"""
调价工作台 Tab — 手动查询单品建议价
"""
from __future__ import annotations
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import AppContext

from ..automation.tasks import _pricing_preview, build_reprice_decision
from ..core.pricing_engine import PricingEngine
from ..core.models import BatchItem, ConfidenceLevel


class CalcTab:
    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        self._event_target = parent
        self._boxes: dict[str, ttk.Combobox] = {}
        self._all_options = {"model": [], "condition": [], "capacity": [], "color": []}
        parent.add(self.frame, text="💰 调价工作台")
        self._build_ui()
        self._refresh_cache_options()
        self._bind_option_events()
        self._event_target.bind("<<SoldCacheUpdated>>", lambda _event: self._refresh_cache_options(), add="+")
        self._event_target.bind("<<ZhuanzhuanSoldCacheUpdated>>", lambda _event: self._refresh_cache_options(), add="+")

    def _build_ui(self):
        input_frame = ttk.LabelFrame(self.frame, text="查询条件")
        input_frame.pack(fill="x", padx=8, pady=6)

        fields = [("model", "型号", 24), ("condition", "成色", 12),
                  ("capacity", "容量", 14), ("color", "颜色", 14)]
        self._vars: dict[str, tk.StringVar] = {}
        for col, (key, label, width) in enumerate(fields):
            ttk.Label(input_frame, text=f"{label}:").grid(row=0, column=col*2, padx=4, pady=6)
            var = tk.StringVar()
            self._vars[key] = var
            box = ttk.Combobox(input_frame, textvariable=var, width=width)
            box.grid(row=0, column=col*2+1, padx=4)
            self._boxes[key] = box

        ttk.Button(input_frame, text="🔍 查询建议价", command=self._query).grid(
            row=0, column=len(fields)*2, padx=8)

        ttk.Label(input_frame, text="当前挂牌价:").grid(row=1, column=0, padx=4, pady=4)
        self._current_price = tk.DoubleVar(value=0)
        ttk.Entry(input_frame, textvariable=self._current_price, width=10).grid(
            row=1, column=1, padx=4)
        ttk.Button(input_frame, text="计算到手价", command=self._calc_settle).grid(
            row=1, column=2, padx=4)

        result_frame = ttk.LabelFrame(self.frame, text="定价建议")
        result_frame.pack(fill="x", padx=8, pady=4)

        kpi_items = [
            ("fast_price",   "极速动销价",   "#1565c0"),
            ("cons_price",   "保守动销价",   "#2e7d32"),
            ("floor_price",  "底价预警",     "#c62828"),
            ("system_price", "系统建议价",   "#ef6c00"),
            ("final_price",  "最终执行价",   "#00838f"),
            ("settle",       "到手价",       "#4a148c"),
            ("sample",       "样本数量",     "gray"),
            ("confidence",   "置信度",       "gray"),
        ]
        self._kpi_vars: dict[str, tk.StringVar] = {}
        for col, (key, label, color) in enumerate(kpi_items):
            f = ttk.Frame(result_frame)
            f.grid(row=0, column=col, padx=20, pady=10)
            ttk.Label(f, text=label, foreground="gray").pack()
            var = tk.StringVar(value="—")
            self._kpi_vars[key] = var
            ttk.Label(f, textvariable=var,
                      font=("微软雅黑", 15, "bold"),
                      foreground=color).pack()

        self._warning_var = tk.StringVar()
        ttk.Label(result_frame, textvariable=self._warning_var,
                  foreground="orange").grid(row=1, column=0, columnspan=6, pady=4)

        explain_frame = ttk.LabelFrame(self.frame, text="规则匹配解释")
        explain_frame.pack(fill="x", padx=8, pady=4)
        self._explain_text = tk.Text(explain_frame, height=4, state="disabled",
                                      font=("Consolas", 9))
        self._explain_text.pack(fill="x", padx=4, pady=4)

        detail_frame = ttk.LabelFrame(self.frame, text="参考成交记录")
        detail_frame.pack(fill="both", expand=True, padx=8, pady=4)

        cols = [("sold_time", "成交时间", 130), ("sold_price", "成交价", 80),
                ("hours", "动销时长(h)", 90), ("source", "来源", 80), ("title", "商品名", 200)]
        vsb = ttk.Scrollbar(detail_frame)
        self._detail_tree = ttk.Treeview(
            detail_frame, columns=[c[0] for c in cols], show="headings",
            yscrollcommand=vsb.set, height=8,
        )
        vsb.config(command=self._detail_tree.yview)
        for col_id, heading, width in cols:
            self._detail_tree.heading(col_id, text=heading)
            self._detail_tree.column(col_id, width=width)
        vsb.pack(side="right", fill="y")
        self._detail_tree.pack(fill="both", expand=True, padx=4, pady=4)

        self._detail_tree.tag_configure("fast", background="#e8f5e9")

    def _bind_option_events(self):
        self._boxes["model"].bind("<KeyRelease>", self._on_model_typing)
        self._boxes["model"].bind("<<ComboboxSelected>>", self._on_model_changed)
        self._boxes["model"].bind("<Return>", self._on_model_confirm)
        self._boxes["model"].bind("<Down>", self._on_model_down)
        self._boxes["model"].bind("<Up>", self._on_model_up)
        self._boxes["model"].bind("<Escape>", lambda _event: self._hide_dropdown("model"))
        self._boxes["condition"].bind("<KeyRelease>", self._on_condition_typing)
        self._boxes["condition"].bind("<<ComboboxSelected>>", self._on_condition_changed)
        self._boxes["capacity"].bind("<KeyRelease>", self._on_capacity_typing)
        self._boxes["capacity"].bind("<<ComboboxSelected>>", self._on_capacity_changed)
        self._boxes["color"].bind("<KeyRelease>", self._refresh_dependent_options)
        self._boxes["color"].bind("<<ComboboxSelected>>", self._refresh_dependent_options)

    def _set_box_values(self, key: str, values: list[str]):
        self._boxes[key]["values"] = values

    def _model_values(self) -> list[str]:
        return list(self._boxes["model"]["values"])

    def _keep_value_if_present(self, key: str, values: list[str]):
        current = self._vars[key].get().strip()
        if current and current not in values:
            self._vars[key].set("")

    def _show_dropdown(self, key: str):
        box = self._boxes[key]
        if not box["values"]:
            return
        try:
            box.tk.call("ttk::combobox::Post", str(box))
        except Exception:
            pass

    def _hide_dropdown(self, key: str):
        box = self._boxes[key]
        try:
            box.tk.call("ttk::combobox::Unpost", str(box))
        except Exception:
            pass

    def _pick_values(self, *groups: list[str]) -> list[str]:
        for values in groups:
            picked: list[str] = []
            seen: set[str] = set()
            for value in values:
                if value and value not in seen:
                    picked.append(value)
                    seen.add(value)
            if picked:
                return picked
        return []

    def _should_use_fuzzy_model(self, model: str | None = None) -> bool:
        current = (model if model is not None else self._vars["model"].get()).strip()
        if not current:
            return False
        return current not in set(self._all_options.get("model", []))

    def _refresh_cache_options(self):
        self._all_options = self.ctx.sold_cache.get_filter_options()
        self._set_box_values("model", self._all_options["model"])
        self._refresh_dependent_options()

    def _refresh_dependent_options(self, _event=None):
        model = self._vars["model"].get().strip()
        condition = self._vars["condition"].get().strip()
        capacity = self._vars["capacity"].get().strip()
        fuzzy_model = self._should_use_fuzzy_model(model)

        filtered = self.ctx.sold_cache.get_filter_options(model=model, fuzzy_model=fuzzy_model)
        condition_values = self._pick_values(
            filtered["condition"],
            self._all_options.get("condition", []),
        )
        self._set_box_values("condition", condition_values)
        self._keep_value_if_present("condition", condition_values)
        condition = self._vars["condition"].get().strip()

        filtered = self.ctx.sold_cache.get_filter_options(
            model=model,
            condition=condition,
            fuzzy_model=fuzzy_model,
        )
        broader_capacity = self.ctx.sold_cache.get_filter_options(
            model=model,
            fuzzy_model=fuzzy_model,
        )
        capacity_values = self._pick_values(
            filtered["capacity"],
            broader_capacity["capacity"],
            self._all_options.get("capacity", []),
        )
        self._set_box_values("capacity", capacity_values)
        self._keep_value_if_present("capacity", capacity_values)
        capacity = self._vars["capacity"].get().strip()

        filtered = self.ctx.sold_cache.get_filter_options(
            model=model,
            condition=condition,
            capacity=capacity,
            fuzzy_model=fuzzy_model,
        )
        broader_color = self.ctx.sold_cache.get_filter_options(
            model=model,
            capacity=capacity,
            fuzzy_model=fuzzy_model,
        )
        model_color = self.ctx.sold_cache.get_filter_options(
            model=model,
            fuzzy_model=fuzzy_model,
        )
        color_values = self._pick_values(
            filtered["color"],
            broader_color["color"],
            model_color["color"],
            self._all_options.get("color", []),
        )
        self._set_box_values("color", color_values)
        self._keep_value_if_present("color", color_values)

    def _select_model_index(self, index: int):
        values = self._model_values()
        if not values:
            return
        index = max(0, min(index, len(values) - 1))
        self._vars["model"].set(values[index])
        try:
            self._boxes["model"].current(index)
        except Exception:
            pass
        self._show_dropdown("model")
        self._refresh_dependent_options()

    def _on_model_confirm(self, _event=None):
        values = self._model_values()
        if values:
            current = self._vars["model"].get().strip()
            if current not in values:
                self._select_model_index(0)
        self._hide_dropdown("model")
        self._query()
        return "break"

    def _on_model_down(self, _event=None):
        values = self._model_values()
        if not values:
            return None
        current = self._vars["model"].get().strip()
        try:
            index = values.index(current)
        except ValueError:
            index = -1
        self._select_model_index(index + 1)
        return "break"

    def _on_model_up(self, _event=None):
        values = self._model_values()
        if not values:
            return None
        current = self._vars["model"].get().strip()
        try:
            index = values.index(current)
        except ValueError:
            index = len(values)
        self._select_model_index(index - 1)
        return "break"

    def _on_model_typing(self, _event=None):
        keyword = self._vars["model"].get().strip().lower()
        all_models = self._all_options.get("model", [])
        filtered = [m for m in all_models if keyword in m.lower()] if keyword else all_models
        self._set_box_values("model", filtered)
        self._refresh_dependent_options()
        if keyword and filtered:
            self._show_dropdown("model")
        else:
            self._hide_dropdown("model")

    def _on_model_changed(self, _event=None):
        self._refresh_dependent_options()

    def _on_condition_typing(self, _event=None):
        self._refresh_dependent_options()

    def _on_condition_changed(self, _event=None):
        self._refresh_dependent_options()

    def _on_capacity_typing(self, _event=None):
        self._refresh_dependent_options()

    def _on_capacity_changed(self, _event=None):
        self._refresh_dependent_options()

    def _query(self):
        model = self._vars["model"].get().strip()
        condition = self._vars["condition"].get().strip()
        capacity = self._vars["capacity"].get().strip()
        color = self._vars["color"].get().strip()
        if not model:
            return

        fuzzy_model = self._should_use_fuzzy_model(model)
        threading.Thread(
            target=self._do_query,
            args=(model, condition, capacity, color, fuzzy_model),
            daemon=True,
        ).start()

    def _do_query(self, model, condition, capacity, color, fuzzy_model):
        records = self.ctx.sold_cache.find_records(
            model=model,
            condition=condition,
            capacity=capacity,
            color=color,
            fuzzy_model=fuzzy_model,
        )
        pricing = self.ctx.pricing_engine.calculate(records, model, condition, capacity, color)
        query_item = BatchItem(
            product_id="", qc_code="", title=model,
            model=model, condition=condition, capacity=capacity, color=color,
            current_price=0, pricing=pricing,
        )
        decision = build_reprice_decision(query_item, pricing, self.ctx.rule_engine, apply_rules=True)
        effective_pricing = decision.get("pricing") or pricing
        lines = decision.get("explain_lines") or []
        preview = _pricing_preview(pricing, self.ctx.rule_engine, query_item, decision)

        def update():
            self._kpi_vars["fast_price"].set(
                f"¥{pricing.fast_price:,.0f}" if pricing.fast_price else "—"
            )
            self._kpi_vars["cons_price"].set(
                f"¥{pricing.cons_price:,.0f}" if pricing.cons_price else "—"
            )
            self._kpi_vars["floor_price"].set(
                f"¥{pricing.floor_price:,.0f}" if pricing.floor_price else "—"
            )
            self._kpi_vars["system_price"].set(
                f"¥{decision['system_price']:,.0f}" if decision.get("system_price") is not None else "—"
            )
            self._kpi_vars["final_price"].set(
                f"¥{decision['final_price']:,.0f}" if decision.get("final_price") is not None else "—"
            )
            self._kpi_vars["sample"].set(str(pricing.sample_count))
            conf_map = {
                ConfidenceLevel.HIGH: "高 ✓",
                ConfidenceLevel.LOW:  "低 ⚠",
                ConfidenceLevel.NONE: "无数据",
            }
            self._kpi_vars["confidence"].set(conf_map.get(pricing.confidence, "—"))

            warning_parts = []
            if fuzzy_model:
                warning_parts.append("当前使用模糊型号匹配")
            if effective_pricing.warning:
                warning_parts.append(effective_pricing.warning)
            warning_parts.append(preview)
            self._warning_var.set("；".join(part for part in warning_parts if part))

            current_price = self._current_price.get()
            if current_price > 0:
                settle = PricingEngine.calc_settle_price(current_price)
            else:
                settle = decision.get("final_settle_price")
            if settle is not None:
                self._kpi_vars["settle"].set(f"¥{settle:,.0f}")
            else:
                self._kpi_vars["settle"].set("—")

            self._explain_text.config(state="normal")
            self._explain_text.delete("1.0", "end")
            self._explain_text.insert("1.0", "\n".join(lines))
            self._explain_text.config(state="disabled")

            for row in self._detail_tree.get_children():
                self._detail_tree.delete(row)
            fast_hours = self.ctx.cfg.fast_sale_hours if hasattr(self.ctx, "cfg") else 24
            for r in sorted(pricing.raw_records, key=lambda x: x.sold_time, reverse=True):
                is_fast = r.hours_to_sell is not None and r.hours_to_sell <= fast_hours
                self._detail_tree.insert("", "end", values=(
                    r.sold_time.strftime("%m-%d %H:%M"),
                    f"¥{r.sold_price:,.0f}",
                    f"{r.hours_to_sell:.1f}" if r.hours_to_sell is not None else "—",
                    r.source,
                    r.title[:30],
                ), tags=("fast",) if is_fast else ())

        self.frame.after(0, update)

    def _calc_settle(self):
        price = self._current_price.get()
        if price > 0:
            settle = PricingEngine.calc_settle_price(price)
            self._kpi_vars["settle"].set(f"¥{settle:,.0f}")
