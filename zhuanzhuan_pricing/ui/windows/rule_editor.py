# -*- coding: utf-8 -*-
"""
规则编辑器 UI — 独立浮窗
供用户增删改查定价规则，改动实时写入 pricing_rules.json
"""
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

from core.rule_engine import RuleEngine, PricingRule, RuleMatch, RuleAction


class RuleEditorWindow(tk.Toplevel):
    """规则配置浮窗，从主界面工具栏或自动化Tab呼出"""

    ACTION_TYPES = {
        "fast_price":         "极速动销价",
        "conservative_price": "保守动销价",
        "pct_drop":           "百分比调整",
        "fixed_drop":         "固定金额调整",
        "fixed_price":        "指定固定价格",
    }

    def __init__(self, master, rule_engine: RuleEngine):
        super().__init__(master)
        self.engine = rule_engine
        self.title("📋 定价规则配置")
        self.geometry("900x580")
        self.resizable(True, True)
        self._build_ui()
        self._refresh_list()

    # ── UI 构建 ───────────────────────────────────────────

    def _build_ui(self):
        # 左侧：规则列表
        left = ttk.Frame(self, width=280)
        left.pack(side="left", fill="y", padx=(8, 0), pady=8)
        left.pack_propagate(False)

        ttk.Label(left, text="规则列表（优先级从高到低）", font=("", 10, "bold")).pack(anchor="w")

        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True, pady=4)

        cols = ("优先级", "名称", "启用")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, [50, 160, 40]):
            self._tree.heading(c, text=c)
            self._tree.column(c, width=w, anchor="center" if c != "名称" else "w")
        sb = ttk.Scrollbar(list_frame, command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        # 列表下方按钮
        btn_row = ttk.Frame(left)
        btn_row.pack(fill="x", pady=2)
        ttk.Button(btn_row, text="➕ 新建", command=self._new_rule, width=7).pack(side="left", padx=2)
        ttk.Button(btn_row, text="🗑 删除", command=self._delete_rule, width=7).pack(side="left", padx=2)
        ttk.Button(btn_row, text="↑", command=self._move_up,   width=3).pack(side="left", padx=1)
        ttk.Button(btn_row, text="↓", command=self._move_down, width=3).pack(side="left", padx=1)

        # 右侧：编辑表单
        right = ttk.LabelFrame(self, text="规则详情")
        right.pack(side="left", fill="both", expand=True, padx=8, pady=8)

        self._form = {}
        form_grid = ttk.Frame(right)
        form_grid.pack(fill="x", padx=8, pady=8)

        def row(label, widget_factory, r):
            ttk.Label(form_grid, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=3)
            w = widget_factory(form_grid)
            w.grid(row=r, column=1, sticky="ew", padx=4, pady=3)
            form_grid.columnconfigure(1, weight=1)
            return w

        self._form["name"]     = row("规则名称", lambda p: ttk.Entry(p, width=28), 0)
        self._form["priority"] = row("优先级（数字越小越高）", lambda p: ttk.Spinbox(p, from_=1, to=99, width=6), 1)
        self._form["enabled"]  = self._make_check(form_grid, "启用规则", 2)

        ttk.Separator(form_grid, orient="horizontal").grid(row=3, columnspan=2, sticky="ew", pady=4)

        # 匹配条件
        ttk.Label(form_grid, text="── 匹配条件 ──", foreground="gray").grid(row=4, columnspan=2, pady=2)
        self._form["model_contains"] = row("型号包含", lambda p: ttk.Entry(p), 5)
        self._form["condition_in"]   = row("成色（逗号分隔）", lambda p: ttk.Entry(p), 6)
        self._form["capacity_in"]    = row("容量（逗号分隔）", lambda p: ttk.Entry(p), 7)
        self._form["stale_days_gte"] = row("滞销天数 ≥", lambda p: ttk.Entry(p, width=8), 8)
        self._form["stale_days_lt"]  = row("滞销天数 <", lambda p: ttk.Entry(p, width=8), 9)
        self._form["account_in"]     = row("账号白名单（逗号分隔，空=全部）", lambda p: ttk.Entry(p), 10)

        ttk.Separator(form_grid, orient="horizontal").grid(row=11, columnspan=2, sticky="ew", pady=4)

        # 执行动作
        ttk.Label(form_grid, text="── 执行动作 ──", foreground="gray").grid(row=12, columnspan=2, pady=2)

        ttk.Label(form_grid, text="动作类型").grid(row=13, column=0, sticky="e", padx=4)
        action_cb = ttk.Combobox(form_grid, values=list(self.ACTION_TYPES.values()),
                                 state="readonly", width=20)
        action_cb.grid(row=13, column=1, sticky="w", padx=4)
        self._form["action_type"] = action_cb

        self._form["adjust_pct"]    = row("百分比调整（如 -2 表示降2%）", lambda p: ttk.Entry(p, width=10), 14)
        self._form["adjust_amount"] = row("固定金额调整（元，负数=降价）", lambda p: ttk.Entry(p, width=10), 15)
        self._form["target_price"]  = row("指定价格（fixed_price 专用）", lambda p: ttk.Entry(p, width=10), 16)
        self._form["note"]          = row("备注说明", lambda p: ttk.Entry(p), 17)

        # 保存按钮
        ttk.Button(right, text="💾 保存规则", command=self._save_rule).pack(pady=8)

        self._editing_index: Optional[int] = None

    def _make_check(self, parent, text, row):
        var = tk.BooleanVar(value=True)
        cb  = ttk.Checkbutton(parent, text=text, variable=var)
        cb.grid(row=row, column=1, sticky="w", padx=4)
        cb._var = var
        return cb

    # ── 列表操作 ──────────────────────────────────────────

    def _refresh_list(self):
        for row in self._tree.get_children():
            self._tree.delete(row)
        for i, rule in enumerate(self.engine.rules):
            tag = "disabled" if not rule.enabled else ""
            self._tree.insert("", "end", iid=str(i),
                              values=(rule.priority, rule.name, "✓" if rule.enabled else "✗"),
                              tags=(tag,))
        self._tree.tag_configure("disabled", foreground="gray")

    def _on_select(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        idx  = int(sel[0])
        rule = self.engine.rules[idx]
        self._editing_index = idx
        self._load_rule_to_form(rule)

    def _load_rule_to_form(self, rule: PricingRule):
        def set_entry(key, val):
            w = self._form[key]
            w.delete(0, "end")
            w.insert(0, str(val) if val is not None else "")

        set_entry("name",     rule.name)
        set_entry("priority", rule.priority)
        self._form["enabled"]._var.set(rule.enabled)

        m = rule.match
        set_entry("model_contains", m.model_contains or "")
        set_entry("condition_in",   ", ".join(m.condition_in))
        set_entry("capacity_in",    ", ".join(m.capacity_in))
        set_entry("stale_days_gte", m.stale_days_gte if m.stale_days_gte is not None else "")
        set_entry("stale_days_lt",  m.stale_days_lt  if m.stale_days_lt  is not None else "")
        set_entry("account_in",     ", ".join(m.account_in))

        a = rule.action
        label = self.ACTION_TYPES.get(a.type, a.type)
        self._form["action_type"].set(label)
        set_entry("adjust_pct",    a.adjust_pct)
        set_entry("adjust_amount", a.adjust_amount)
        set_entry("target_price",  a.target_price if a.target_price is not None else "")
        set_entry("note", rule.note)

    def _save_rule(self):
        try:
            rule = self._form_to_rule()
        except ValueError as e:
            messagebox.showerror("输入错误", str(e), parent=self)
            return

        if self._editing_index is None:
            self.engine.add_rule(rule)
        else:
            self.engine.update_rule(self._editing_index, rule)
        self._refresh_list()
        messagebox.showinfo("保存成功", f"规则「{rule.name}」已保存", parent=self)

    def _form_to_rule(self) -> PricingRule:
        def get(key): return self._form[key].get().strip()

        name = get("name")
        if not name:
            raise ValueError("规则名称不能为空")

        # 动作类型反查
        label = get("action_type")
        action_type = next(
            (k for k, v in self.ACTION_TYPES.items() if v == label),
            "fast_price"
        )

        def parse_int(key):
            v = get(key)
            return int(v) if v else None

        def parse_float(key):
            v = get(key)
            return float(v) if v else 0.0

        def parse_list(key):
            v = get(key)
            return [x.strip() for x in v.split(",") if x.strip()] if v else []

        return PricingRule(
            name     = name,
            enabled  = self._form["enabled"]._var.get(),
            priority = int(get("priority") or 99),
            note     = get("note"),
            match    = RuleMatch(
                model_contains = get("model_contains") or None,
                condition_in   = parse_list("condition_in"),
                capacity_in    = parse_list("capacity_in"),
                stale_days_gte = parse_int("stale_days_gte"),
                stale_days_lt  = parse_int("stale_days_lt"),
                account_in     = parse_list("account_in"),
            ),
            action   = RuleAction(
                type          = action_type,
                adjust_pct    = parse_float("adjust_pct"),
                adjust_amount = parse_float("adjust_amount"),
                target_price  = float(get("target_price")) if get("target_price") else None,
            ),
        )

    def _new_rule(self):
        self._editing_index = None
        for key, w in self._form.items():
            if hasattr(w, "delete"):
                w.delete(0, "end")
        self._form["enabled"]._var.set(True)
        self._form["priority"].delete(0, "end")
        self._form["priority"].insert(0, "50")

    def _delete_rule(self):
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        name = self.engine.rules[idx].name
        if messagebox.askyesno("确认删除", f"删除规则「{name}」？", parent=self):
            self.engine.delete_rule(idx)
            self._editing_index = None
            self._refresh_list()

    def _move_up(self):
        sel = self._tree.selection()
        if sel:
            self.engine.move_up(int(sel[0]))
            self._refresh_list()

    def _move_down(self):
        sel = self._tree.selection()
        if sel:
            self.engine.move_down(int(sel[0]))
            self._refresh_list()
