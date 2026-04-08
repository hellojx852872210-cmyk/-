# -*- coding: utf-8 -*-
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget

from .tab_batch import ZhuanzhuanBatchQtTab
from .tab_calc import ZhuanzhuanCalcQtTab
from .tab_fetch import ZhuanzhuanFetchQtTab
from .tab_imei import ZhuanzhuanImeiQtTab


class ZhuanzhuanQtTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.platform_ctx = ctx.zhuanzhuan
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel("转转平台：先落地 browser-backed fetch 子页，保持其它业务边界不变。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        notebook = QTabWidget()
        layout.addWidget(notebook, 1)
        self._notebook = notebook

        self._safe_add_tab("fetch", lambda: ZhuanzhuanFetchQtTab(self.platform_ctx), "⚙️ 数据管理")
        self._safe_add_tab("calc", lambda: ZhuanzhuanCalcQtTab(self.platform_ctx), "🧮 定价测算")
        self._safe_add_tab("imei", lambda: ZhuanzhuanImeiQtTab(self.platform_ctx), "🔎 IMEI 查询")
        self._safe_add_tab("batch", lambda: ZhuanzhuanBatchQtTab(self.platform_ctx), "📦 批量处理")

    def _safe_add_tab(self, tab_name: str, factory, label: str):
        try:
            widget = factory()
        except Exception as exc:
            widget = self._error_pane(tab_name, str(exc))
        self._notebook.addTab(widget, label)

    def _safe_add_placeholder(self, tab_name: str, label: str, text: str):
        self._safe_add_tab(tab_name, lambda: self._placeholder(text), label)

    def _placeholder(self, text: str):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)
        return widget

    def _error_pane(self, tab_name: str, detail: str):
        return self._placeholder(f"{tab_name} 初始化失败，但不会影响其它子页。\n\n{detail}")
