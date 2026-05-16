# -*- coding: utf-8 -*-
"""
转转平台容器 Tab
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import AppContext


class ZhuanzhuanTab:
    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.platform_ctx = ctx.zhuanzhuan
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="🏪 转转")
        self._build_ui()

    def _build_ui(self):
        header = ttk.Frame(self.frame)
        header.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(header, text="转转平台", font=("微软雅黑", 13, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="将原先分散在多个顶层页签的转转能力统一收口到这里，先复用原有页面逻辑，再逐步清理重复入口。",
            foreground="gray",
        ).pack(anchor="w", pady=(2, 0))

        self._notebook = ttk.Notebook(self.frame)
        self._notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        from .tab_fetch import FetchTab
        from .tab_calc import CalcTab
        from .tab_imei import ImeiTab
        from .tab_batch import BatchTab

        self._subtabs = {}
        for name, tab_class in (
            ("fetch", FetchTab),
            ("calc", CalcTab),
            ("imei", ImeiTab),
            ("batch", BatchTab),
        ):
            self._subtabs[name] = self._safe_create_subtab(name, tab_class)

    def _safe_create_subtab(self, tab_name: str, tab_class):
        try:
            return tab_class(self._notebook, self.platform_ctx)
        except Exception as exc:
            return _ZhuanzhuanSubtabInitErrorPane(self._notebook, tab_name=tab_name, detail=str(exc))


class _ZhuanzhuanSubtabInitErrorPane:
    def __init__(self, parent: ttk.Notebook, *, tab_name: str, detail: str):
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text=f"⚠️ {tab_name}")
        card = ttk.LabelFrame(self.frame, text=f"转转子模块初始化失败：{tab_name}")
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(
            card,
            text="该子模块当前不可用，但不会阻塞其它转转子模块。",
            foreground="#b26a00",
        ).pack(anchor="w", padx=12, pady=(12, 6))
        text = tk.Text(card, height=10, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        text.insert("1.0", detail or "未知错误")
        text.config(state="disabled")
