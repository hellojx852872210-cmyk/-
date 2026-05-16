# -*- coding: utf-8 -*-
"""改价历史查看器 — 独立浮窗，支持多维筛选和导出"""
import csv
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.price_history import PriceHistoryStore

from core.models import PriceTrigger


class HistoryViewerWindow(tk.Toplevel):
    """改价历史查看浮窗"""

    TRIGGER_LABELS = {
        "": "全部",
        **{t.value: t.label for t in PriceTrigger},
    }
    COLUMNS = [
        ("时间",   "timestamp",    130),
        ("账号",   "account_name",  80),
        ("型号",   "model",        100),
        ("成色",   "condition",     60),
        ("容量",   "capacity",      60),
        ("商品名", "title",        160),
        ("旧价",   "old_price",     65),
        ("新价",   "new_price",     65),
        ("变动",   "diff",          65),
        ("来源",   "trigger",       80),
    ]

    def __init__(self, master, history_store: "PriceHistoryStore"):
        super().__init__(master)
        self.history = history_store
        self.title("📜 改价历史记录")
        self.geometry("1050x560")
        self.resizable(True, True)
        self._rows = []
        self._build_ui()
        self._query()

    # ── UI 构建 ───────────────────────────────────────────

    def _build_ui(self):
        # 筛选栏
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)

        ttk.Label(bar, text="近").pack(side="left")
        self._days_var = tk.IntVar(value=7)
        ttk.Spinbox(bar, from_=1, to=365, textvariable=self._days_var, width=4).pack(side="left")
        ttk.Label(bar, text="天").pack(side="left", padx=(0, 8))

        ttk.Label(bar, text="账号：").pack(side="left")
        self._acct_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self._acct_var, width=10).pack(side="left")

        ttk.Label(bar, text="  型号：").pack(side="left")
        self._model_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self._model_var, width=12).pack(side="left")

        ttk.Label(bar, text="  来源：").pack(side="left")
        self._trigger_var = tk.StringVar(value="全部")
        ttk.Combobox(
            bar, textvariable=self._trigger_var,
            values=list(self.TRIGGER_LABELS.values()),
            state="readonly", width=10,
        ).pack(side="left")

        ttk.Button(bar, text="🔍 查询", command=self._query).pack(side="left", padx=8)
        ttk.Button(bar, text="📥 导出CSV", command=self._export).pack(side="left")

        # 统计摘要
        self._summary_var = tk.StringVar()
        ttk.Label(bar, textvariable=self._summary_var, foreground="gray").pack(side="right", padx=8)

        # 表格
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self._tree = ttk.Treeview(
            tree_frame,
            columns=[c[0] for c in self.COLUMNS],
            show="headings",
            selectmode="browse",
        )
        for label, _, width in self.COLUMNS:
            self._tree.heading(label, text=label)
            self._tree.column(label, width=width, anchor="center" if label not in ("商品名", "型号") else "w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # 行色区分：降价=浅红，涨价=浅绿
        self._tree.tag_configure("drop",  background="#FFF0F0")
        self._tree.tag_configure("rise",  background="#F0FFF0")
        self._tree.tag_configure("zero",  background="#FAFAFA")

    # ── 数据操作 ──────────────────────────────────────────

    def _query(self):
        days    = self._days_var.get()
        account = self._acct_var.get().strip() or None
        model   = self._model_var.get().strip() or None

        # 反查触发来源枚举值
        trigger_label = self._trigger_var.get()
        trigger = next(
            (k for k, v in self.TRIGGER_LABELS.items() if v == trigger_label and k),
            None,
        )
        trigger_enum = next((t for t in PriceTrigger if t.value == trigger), None) if trigger else None

        self._rows = self.history.query(
            days=days, account_name=account, model=model, trigger=trigger_enum, limit=1000,
        )
        self._render()

    def _render(self):
        for row in self._tree.get_children():
            self._tree.delete(row)

        trigger_labels = {t.value: t.label for t in PriceTrigger}

        for r in self._rows:
            diff = r.get("diff", 0)
            tag  = "drop" if diff < 0 else ("rise" if diff > 0 else "zero")
            values = [
                r.get("timestamp", "")[:19],
                r.get("account_name", ""),
                r.get("model", ""),
                r.get("condition", ""),
                r.get("capacity", ""),
                r.get("title", "")[:20],
                f"{r.get('old_price', 0):.0f}",
                f"{r.get('new_price', 0):.0f}",
                f"{diff:+.0f}",
                trigger_labels.get(r.get("trigger", ""), r.get("trigger", "")),
            ]
            self._tree.insert("", "end", values=values, tags=(tag,))

        total = len(self._rows)
        drops = sum(1 for r in self._rows if r.get("diff", 0) < 0)
        rises = sum(1 for r in self._rows if r.get("diff", 0) > 0)
        self._summary_var.set(f"共 {total} 条 | 降价 {drops} | 涨价 {rises}")

    def _export(self):
        if not self._rows:
            messagebox.showinfo("提示", "没有可导出的数据", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
            initialfile="price_history_export.csv",
        )
        if not path:
            return
        fieldnames = [c[1] for c in self.COLUMNS]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._rows)
        messagebox.showinfo("导出成功", f"已保存到\n{path}", parent=self)
