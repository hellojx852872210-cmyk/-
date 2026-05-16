# -*- coding: utf-8 -*-
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget

from ..ui.app import AppContext
from .tab_history import HistoryQtTab
from .tab_rules import RulesQtTab
from .tab_same_sale import SameSaleQtTab
from .tab_zhuanzhuan import ZhuanzhuanQtTab
from .manual_review_window import ManualReviewDialog


class MainWindow(QMainWindow):
    """Qt 主窗口（默认/规范入口）。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("多平台同售管理中枢 v2.0")
        self.resize(1280, 800)
        self.ctx = AppContext()
        self.ctx.register_auto_tasks()
        self._manual_review_dialog: ManualReviewDialog | None = None
        self._build_ui()
        self._build_menu()
        self.ctx.scheduler.start()

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        notebook = QTabWidget()
        layout.addWidget(notebook)
        self.setCentralWidget(root)
        self._notebook = notebook

        self._safe_add_tab("zhuanzhuan", lambda: ZhuanzhuanQtTab(self.ctx), "🏪 转转")
        self._safe_add_placeholder("paipai", "🛍️ 拍拍", "当前阶段先保留原模块边界，后续迁 Qt 原生页。")
        self._safe_add_placeholder("xianyu", "🐟 闲鱼", "当前阶段为占位页。")
        self._safe_add_placeholder("95fen", "👟 95分", "当前阶段为占位页。")
        self._safe_add_tab("rules", lambda: RulesQtTab(self.ctx), "🧭 规则中心")
        self._safe_add_tab("same_sale", lambda: SameSaleQtTab(self.ctx), "🔗 同售管理")
        self._safe_add_placeholder("dashboard", "📊 数据看板", "数据看板后续迁 Qt + matplotlib。")
        self._safe_add_tab("history", lambda: HistoryQtTab(self.ctx), "📋 历史记录")

    def _build_menu(self):
        tools_menu = self.menuBar().addMenu("工具")
        manual_review_action = tools_menu.addAction("手动确认调价")
        manual_review_action.triggered.connect(self._open_manual_review_window)
        bridge_action = tools_menu.addAction("打开 Bridge GUI")
        bridge_action.triggered.connect(self._open_bridge_gui)

    def _open_manual_review_window(self):
        if self._manual_review_dialog is None:
            self._manual_review_dialog = ManualReviewDialog(self.ctx, self)
            self._manual_review_dialog.finished.connect(self._on_manual_review_window_closed)
        self._manual_review_dialog.refresh()
        self._manual_review_dialog.show()
        self._manual_review_dialog.raise_()
        self._manual_review_dialog.activateWindow()

    def _on_manual_review_window_closed(self, _result: int):
        self._manual_review_dialog = None

    def _open_bridge_gui(self):
        project_root = Path(__file__).resolve().parents[2]
        try:
            subprocess.Popen(
                [sys.executable, "-m", "zhuanzhuan_pricing.bridge_gui"],
                cwd=str(project_root),
            )
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", f"启动 Bridge GUI 失败:\n{exc}")

    def _safe_add_tab(self, tab_name: str, factory, label: str):
        try:
            widget = factory()
        except Exception as exc:
            widget = self._error_pane(tab_name, str(exc))
        self._notebook.addTab(widget, label)

    def _safe_add_placeholder(self, tab_name: str, label: str, text: str):
        self._safe_add_tab(tab_name, lambda: self._info_pane(text), label)

    def _info_pane(self, text: str):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)
        return widget

    def _error_pane(self, tab_name: str, detail: str):
        return self._info_pane(f"{tab_name} 初始化失败，但不会阻塞其它顶层模块。\n\n{detail}")

    def closeEvent(self, event):
        try:
            self.ctx.scheduler.stop()
        finally:
            super().closeEvent(event)
