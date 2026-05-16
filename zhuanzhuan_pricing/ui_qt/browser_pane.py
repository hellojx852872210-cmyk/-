# -*- coding: utf-8 -*-
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class BrowserPane(QWidget):
    def __init__(self, session, *, title: str, start_url: str = "https://www.zhuanzhuan.com/"):
        super().__init__()
        self.session = session
        self.title = title
        self.start_url = start_url
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        title = QLabel(f"{self.title} · 独立浏览器会话：{self.session.profile_key}")
        status = QLabel("该页面使用独立 QWebEngineProfile，Cookie / LocalStorage / Session 会持久化到本地。")
        title.setWordWrap(True)
        status.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(status)

        view = self.session.create_view()
        view.load(QUrl(self.start_url))
        layout.addWidget(view, 1)
        self.view = view
