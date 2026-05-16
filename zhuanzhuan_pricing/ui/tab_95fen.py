# -*- coding: utf-8 -*-
"""
95分平台占位 Tab
"""
from __future__ import annotations

from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import AppContext


class Fen95Tab:
    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="👟 95分")
        self._build_ui()

    def _build_ui(self):
        card = ttk.LabelFrame(self.frame, text="平台接入状态")
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(card, text="95分", font=("微软雅黑", 13, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        ttk.Label(card, text="当前状态：未接入（占位页）", foreground="#b26a00").pack(anchor="w", padx=12)
        ttk.Label(
            card,
            text=(
                "后续可在此接入：\n"
                "• 平台配置与账号校验\n"
                "• 商品同步与同售关联\n"
                "• 改价 / 下架执行\n"
                "• 平台规则与运行日志"
            ),
            justify="left",
            foreground="gray",
        ).pack(anchor="w", padx=12, pady=(12, 12))
