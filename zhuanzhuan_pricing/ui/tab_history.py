# -*- coding: utf-8 -*-
"""
改价历史记录 Tab
"""
from __future__ import annotations
import csv
import datetime
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from .app import AppContext

from ..core.models import PriceChangeRecord


class HistoryTab:
    """改价历史查询"""

    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="📋 改价历史")
        self._records: List[PriceChangeRecord] = []
        self._build_ui()

    def _build_ui(self):
        # 筛选栏
        filter_frame = ttk.LabelFrame(self.frame, text="筛选条件")
        filter_frame.pack(fill="x", padx=8, pady=4)

        ttk.Label(filter_frame, text="型号:").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self._model_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self._model_var, width=18).grid(row=0, column=1, padx=4)

        ttk.Label(filter_frame, text="账号:").grid(row=0, column=2, padx=4)
        self._account_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self._account_var, width=14).grid(row=0, column=3, padx=4)

        ttk.Label(filter_frame, text="触发方式:").grid(row=0, column=4, padx=4)
        self._trigger_var = tk.StringVar()
        ttk.Combobox(
            filter_frame, textvariable=self._trigger_var, width=14,
            values=["", "manual", "auto_reprice", "auto_stale", "auto_list", "wxapp_cmd"],
            state="readonly",
        ).grid(row=0, column=5, padx=4)

        ttk.Label(filter_frame, text="近 N 天:").grid(row=0, column=6, padx=4)
        self._days_var = tk.IntVar(value=30)
        ttk.Spinbox(filter_frame, from_=1, to=365, textvariable=self._days_var,
                    width=6).grid(row=0, column=7, padx=4)

        ttk.Button(filter_frame, text="🔍 查询", command=self._query).grid(row=0, column=8, padx=8)
        ttk.Button(filter_frame, text="📤 导出 CSV", command=self._export_csv).grid(row=0, column=9, padx=4)

        # Treeview
        cols = [
            ("timestamp",    "改价时间",   130),
            ("qc_code",      "质检码",      90),
            ("title",        "商品名",     160),
            ("model",        "型号",       100),
            ("old_price",    "改前价",      75),
            ("new_price",    "改后价",      75),
            ("diff",         "差价",        65),
            ("diff_pct",     "降幅%",       60),
            ("settle_price", "改后预计到手",  96),
            ("trigger",      "触发",        90),
            ("account_name", "账号",        80),
            ("note",         "备注",       120),
        ]
        tree_frame = ttk.Frame(self.frame)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal")
        self._tree = ttk.Treeview(
            tree_frame, columns=[c[0] for c in cols], show="headings",
            yscrollcommand=vsb.set, xscrollcommand=hsb.set,
        )
        vsb.config(command=self._tree.yview)
        hsb.config(command=self._tree.xview)

        for col_id, heading, width in cols:
            self._tree.heading(col_id, text=heading)
            self._tree.column(col_id, width=width, minwidth=40)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # 颜色
        self._tree.tag_configure("drop",  background="#fff8e8")
        self._tree.tag_configure("raise", background="#f2f8f2")

        # 状态栏
        self._status_var = tk.StringVar(value="点击「查询」加载记录")
        ttk.Label(self.frame, textvariable=self._status_var, foreground="gray").pack(pady=3)

    def _query(self):
        self._status_var.set("查询中 …")
        threading.Thread(target=self._do_query, daemon=True).start()

    def _do_query(self):
        try:
            records = self.ctx.history_db.query(
                model=self._model_var.get(),
                account=self._account_var.get(),
                trigger=self._trigger_var.get(),
                days=self._days_var.get(),
                limit=1000,
            )
            self._records = records
            self.frame.after(0, lambda: self._render(records))
        except Exception as e:
            self.frame.after(0, lambda e=e: self._status_var.set(f"查询失败: {e}"))

    def _render(self, records: List[PriceChangeRecord]):
        for row in self._tree.get_children():
            self._tree.delete(row)

        for r in records:
            tag = "drop" if r.diff < 0 else ("raise" if r.diff > 0 else "")
            trigger_label = {
                "manual":       "手动",
                "auto_reprice": "自动调价",
                "auto_stale":   "滞销降价",
                "auto_list":    "自动上架",
                "wxapp_cmd":    "微信指令",
            }.get(r.trigger.value if hasattr(r.trigger, "value") else r.trigger, str(r.trigger))

            self._tree.insert("", "end", values=(
                r.timestamp.strftime("%m-%d %H:%M"),
                r.qc_code,
                r.title[:20],
                r.model[:14],
                f"¥{r.old_price:,.0f}",
                f"¥{r.new_price:,.0f}",
                f"{r.diff:+.0f}",
                f"{r.diff_pct:+.1f}%",
                f"¥{r.settle_price:,.0f}",
                trigger_label,
                r.account_name,
                r.note,
            ), tags=(tag,) if tag else ())

        self._status_var.set(f"共 {len(records)} 条记录")

    def _export_csv(self):
        if not self._records:
            self._query()
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
            initialfile=f"改价历史_{datetime.date.today()}.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "时间", "质检码", "商品名", "型号", "成色", "容量", "颜色",
                "改前价", "改后价", "差价", "降幅%", "改后预计到手",
                "触发方式", "账号", "备注",
            ])
            for r in self._records:
                writer.writerow([
                    r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    r.qc_code, r.title, r.model, r.condition, r.capacity, r.color,
                    r.old_price, r.new_price, r.diff,
                    f"{r.diff_pct:.2f}", r.settle_price,
                    r.trigger.value if hasattr(r.trigger, "value") else r.trigger,
                    r.account_name, r.note,
                ])
        self._status_var.set(f"已导出 {path}")
