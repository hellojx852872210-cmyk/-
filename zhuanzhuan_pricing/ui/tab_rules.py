# -*- coding: utf-8 -*-
"""
规则引擎配置 Tab — 增删改规则
（嵌入在「自动化」Tab 内，或独立弹窗均可）
"""
from __future__ import annotations
import json
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import AppContext

from ..core.models import PricingRule


class RulesCenterTab:
    """规则中心主页面"""

    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="🧭 规则中心")
        self._build_ui()

    def _build_ui(self):
        intro = ttk.LabelFrame(self.frame, text="规则中心")
        intro.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Label(
            intro,
            text="统一承载跨平台定价规则。当前先复用现有规则编辑器，后续可继续扩展回测、命中统计与平台差异化规则。",
            foreground="gray",
            justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 8))
        ttk.Button(intro, text="⚙️ 打开规则编辑器", command=self._open_editor).pack(anchor="w", padx=10, pady=(0, 10))

    def _open_editor(self):
        RulesEditor(self.frame.winfo_toplevel(), self.ctx)


class RulesEditor(tk.Toplevel):
    """规则编辑器浮窗"""

    def __init__(self, parent, ctx: "AppContext"):
        super().__init__(parent)
        self.ctx = ctx
        self.title("⚙️ 定价规则配置")
        self.geometry("860x540")
        self.resizable(True, True)
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        # 左：规则列表
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=8, pady=8)

        ttk.Label(left, text="规则列表（按优先级）", font=("微软雅黑", 10, "bold")).pack()
        self._listbox = tk.Listbox(left, width=30, selectmode="single")
        self._listbox.pack(fill="y", expand=True)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)

        btn_frame = ttk.Frame(left)
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="➕ 新增", command=self._new_rule).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="🗑️ 删除", command=self._delete_rule).pack(side="left", padx=2)

        # 右：规则编辑表单
        right = ttk.LabelFrame(self, text="规则详情")
        right.pack(side="left", fill="both", expand=True, padx=8, pady=8)

        form_fields = [
            ("name",     "规则名称",   "entry"),
            ("priority", "优先级（越小越先）", "spinbox"),
            ("enabled",  "是否启用",   "check"),
            ("note",     "备注",       "entry"),
        ]

        self._vars: dict[str, tk.Variable] = {}
        for row, (key, label, widget_type) in enumerate(form_fields):
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="w",
                                               padx=6, pady=4)
            if widget_type == "entry":
                var = tk.StringVar()
                ttk.Entry(right, textvariable=var, width=36).grid(
                    row=row, column=1, sticky="ew", padx=6)
            elif widget_type == "spinbox":
                var = tk.IntVar(value=50)
                ttk.Spinbox(right, from_=1, to=999, textvariable=var, width=8).grid(
                    row=row, column=1, sticky="w", padx=6)
            elif widget_type == "check":
                var = tk.BooleanVar(value=True)
                ttk.Checkbutton(right, variable=var).grid(
                    row=row, column=1, sticky="w", padx=6)
            self._vars[key] = var

        # 匹配条件（JSON）
        ttk.Label(right, text="匹配条件（JSON）").grid(
            row=len(form_fields), column=0, sticky="nw", padx=6, pady=4)
        self._match_text = tk.Text(right, width=40, height=5,
                                   font=("Consolas", 10))
        self._match_text.grid(row=len(form_fields), column=1, sticky="ew",
                               padx=6, pady=4)

        # 执行动作（JSON）
        ttk.Label(right, text="执行动作（JSON）").grid(
            row=len(form_fields)+1, column=0, sticky="nw", padx=6, pady=4)
        self._action_text = tk.Text(right, width=40, height=5,
                                    font=("Consolas", 10))
        self._action_text.grid(row=len(form_fields)+1, column=1,
                                sticky="ew", padx=6, pady=4)

        right.columnconfigure(1, weight=1)

        # 帮助文本
        help_text = (
            "匹配条件可用字段：\n"
            "  model_contains: \"iPhone\"\n"
            "  condition_in: [\"99新\",\"95新\"]\n"
            "  stale_days_gte: 7\n"
            "  price_gte: 1000\n\n"
            "执行动作类型：\n"
            "  fast_price / conservative_price\n"
            "  fixed_drop {amount:100}\n"
            "  pct_drop   {pct:5}\n"
            "  fixed_price {amount:1000}"
        )
        ttk.Label(right, text=help_text, foreground="gray",
                  justify="left", font=("微软雅黑", 8)).grid(
            row=len(form_fields)+2, column=0, columnspan=2,
            sticky="w", padx=6, pady=4)

        # 保存按钮
        ttk.Button(right, text="💾 保存规则", command=self._save_rule).grid(
            row=len(form_fields)+3, column=1, sticky="e", padx=6, pady=8)

        self._selected_index: int = -1

    # ── 列表操作 ─────────────────────────────────────────────

    def _refresh_list(self):
        self._listbox.delete(0, "end")
        for rule in self.ctx.rule_engine.rules:
            prefix = "✅" if rule.enabled else "⬜"
            self._listbox.insert("end", f"{prefix} [{rule.priority}] {rule.name}")

    def _on_select(self, _event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self._selected_index = idx
        rule = self.ctx.rule_engine.rules[idx]

        self._vars["name"].set(rule.name)
        self._vars["priority"].set(rule.priority)
        self._vars["enabled"].set(rule.enabled)
        self._vars["note"].set(rule.note)

        self._match_text.delete("1.0", "end")
        self._match_text.insert("1.0", json.dumps(rule.match, ensure_ascii=False, indent=2))

        self._action_text.delete("1.0", "end")
        self._action_text.insert("1.0", json.dumps(rule.action, ensure_ascii=False, indent=2))

    def _save_rule(self):
        try:
            match  = json.loads(self._match_text.get("1.0", "end").strip() or "{}")
            action = json.loads(self._action_text.get("1.0", "end").strip() or "{}")
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON 格式错误", str(e))
            return

        rule = PricingRule(
            name=self._vars["name"].get(),
            enabled=self._vars["enabled"].get(),
            priority=int(self._vars["priority"].get()),
            match=match,
            action=action,
            note=self._vars["note"].get(),
        )

        if self._selected_index >= 0:
            self.ctx.rule_engine.update_rule(self._selected_index, rule)
        else:
            self.ctx.rule_engine.add_rule(rule)

        self._refresh_list()
        messagebox.showinfo("保存", "规则已保存")

    def _new_rule(self):
        self._selected_index = -1
        for key, var in self._vars.items():
            if isinstance(var, tk.BooleanVar):
                var.set(True)
            elif isinstance(var, tk.IntVar):
                var.set(50)
            else:
                var.set("")
        self._match_text.delete("1.0", "end")
        self._match_text.insert("1.0", '{\n  "model_contains": ""\n}')
        self._action_text.delete("1.0", "end")
        self._action_text.insert("1.0", '{\n  "type": "fast_price",\n  "adjust_pct": 0\n}')

    def _delete_rule(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        rule = self.ctx.rule_engine.rules[idx]
        if messagebox.askyesno("删除", f"确认删除规则「{rule.name}」？"):
            self.ctx.rule_engine.delete_rule(idx)
            self._selected_index = -1
            self._refresh_list()
