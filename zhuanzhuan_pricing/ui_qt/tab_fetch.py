# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QEventLoop, QObject, QTimer, Qt, QUrl, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Account, ConfidenceLevel
from ..services.paipai_service import PaipaiConfig
from ..services.zhuanzhuan_api import DataFetcher, ImeiService
from .browser_pane import BrowserPane


class _CookieCollector(QObject):
    cookie_received = Signal(object)

    def __init__(self):
        super().__init__()
        self.cookies = []

    def on_cookie_added(self, cookie):
        self.cookies.append(cookie)
        self.cookie_received.emit(cookie)


class FetchBrowserWindow(QMainWindow):
    def __init__(self, session, *, window_title: str, pane_title: str):
        super().__init__()
        self.setWindowTitle(window_title)
        self.resize(1180, 860)
        self.browser = BrowserPane(
            session,
            title=pane_title,
            start_url="https://www.zhuanzhuan.com/",
        )
        self.setCentralWidget(self.browser)

    def closeEvent(self, event: QCloseEvent):
        self.hide()
        event.ignore()


class ZhuanzhuanFetchQtTab(QWidget):
    SYNC_RANGE_OPTIONS = {
        "全部": 0,
        "近 7 天": 7,
        "近 14 天": 14,
        "近 30 天": 30,
        "近 90 天": 90,
    }
    BROWSE_RANGE_OPTIONS = {
        "全部": 0,
        "近 7 天": 7,
        "近 14 天": 14,
        "近 30 天": 30,
        "近 90 天": 90,
    }

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._browser_windows: dict[str, FetchBrowserWindow] = {}
        self._current_instance_id: str | None = None
        self._account_test_in_progress = False
        self._account_test_result = None
        self._sync_in_progress = False
        self._sync_result = None
        self._build_ui()
        self._ensure_default_browser_instance()
        self._refresh_browser_instances()
        self._refresh_accounts()
        self._refresh_cache_filters()
        self._refresh_cache_view()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "该页面用于管理转转登录会话、账号 Cookie、本地成交缓存采集与基础定价分析。现在可为不同店铺维护彼此隔离的浏览器实例。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        sidebar = self._build_sidebar()
        scroll.setWidget(sidebar)
        layout.addWidget(scroll, 1)

    def _build_sidebar(self):
        sidebar = QWidget()
        layout = QVBoxLayout(sidebar)

        platform_row = QHBoxLayout()
        platform_row.addWidget(QLabel("当前平台"))
        self._platform_combo = QComboBox()
        self._platform_combo.addItem("转转", "zhuanzhuan")
        self._platform_combo.addItem("拍拍", "paipai")
        self._platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        platform_row.addWidget(self._platform_combo)
        platform_row.addStretch(1)
        layout.addLayout(platform_row)

        layout.addWidget(self._build_profile_box())
        layout.addWidget(self._build_account_box())
        layout.addWidget(self._build_sync_box())
        layout.addWidget(self._build_cache_box(), 1)

        return sidebar

    def _build_profile_box(self):
        box = QGroupBox("浏览器会话")
        layout = QVBoxLayout(box)

        hint = QLabel("每个浏览器实例都有独立 QWebEngineProfile；Cookie / LocalStorage / Session 会各自持久化到固定目录。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        content = QHBoxLayout()

        left = QVBoxLayout()
        self._browser_list = QListWidget()
        self._browser_list.itemSelectionChanged.connect(self._on_browser_instance_selected)
        left.addWidget(self._browser_list)

        btn_grid = QGridLayout()
        new_btn = QPushButton("新建浏览器")
        open_btn = QPushButton("打开浏览器")
        rename_btn = QPushButton("重命名")
        delete_btn = QPushButton("删除")
        default_btn = QPushButton("设为默认")
        btn_grid.addWidget(new_btn, 0, 0)
        btn_grid.addWidget(open_btn, 0, 1)
        btn_grid.addWidget(rename_btn, 1, 0)
        btn_grid.addWidget(delete_btn, 1, 1)
        btn_grid.addWidget(default_btn, 2, 0, 1, 2)
        left.addLayout(btn_grid)
        content.addLayout(left, 2)

        right = QVBoxLayout()
        meta_form = QFormLayout()
        self._browser_name_label = QLabel("—")
        self._browser_name_label.setWordWrap(True)
        self._browser_id_label = QLabel("—")
        self._browser_id_label.setWordWrap(True)
        self._browser_profile_key_label = QLabel("—")
        self._browser_profile_key_label.setWordWrap(True)
        self._browser_profile_dir_label = QLabel("—")
        self._browser_profile_dir_label.setWordWrap(True)
        self._browser_cookie_status_label = QLabel("—")
        self._browser_cookie_status_label.setWordWrap(True)
        meta_form.addRow("实例名", self._browser_name_label)
        meta_form.addRow("实例 ID", self._browser_id_label)
        meta_form.addRow("Profile Key", self._browser_profile_key_label)
        meta_form.addRow("Profile Dir", self._browser_profile_dir_label)
        meta_form.addRow("会话 Cookie", self._browser_cookie_status_label)
        right.addLayout(meta_form)

        nav_row = QHBoxLayout()
        self._url_input = QLineEdit("https://www.zhuanzhuan.com/")
        go_btn = QPushButton("打开")
        reload_btn = QPushButton("刷新")
        nav_row.addWidget(self._url_input, 1)
        nav_row.addWidget(go_btn)
        nav_row.addWidget(reload_btn)
        right.addLayout(nav_row)

        self._browser_hint = QLabel("先选中一个浏览器实例，再打开窗口。")
        self._browser_hint.setWordWrap(True)
        right.addWidget(self._browser_hint)

        self._browser_cookie_output = QPlainTextEdit()
        self._browser_cookie_output.setPlaceholderText("这里会显示当前浏览器实例导出的 Cookie，可按需手动删改")
        self._browser_cookie_output.setMaximumHeight(100)
        right.addWidget(self._browser_cookie_output)

        cookie_btn_row = QHBoxLayout()
        export_cookie_btn = QPushButton("获取 Cookie")
        use_cookie_btn = QPushButton("回填到账号")
        cookie_btn_row.addWidget(export_cookie_btn)
        cookie_btn_row.addWidget(use_cookie_btn)
        right.addLayout(cookie_btn_row)
        content.addLayout(right, 3)

        layout.addLayout(content)

        new_btn.clicked.connect(self._create_browser_instance)
        open_btn.clicked.connect(self._show_browser_window)
        rename_btn.clicked.connect(self._rename_browser_instance)
        delete_btn.clicked.connect(self._delete_browser_instance)
        default_btn.clicked.connect(self._set_default_browser_instance)
        export_cookie_btn.clicked.connect(self._export_browser_cookies)
        use_cookie_btn.clicked.connect(self._apply_browser_cookies_to_account)
        go_btn.clicked.connect(self._open_url)
        reload_btn.clicked.connect(self._reload_browser)
        self._url_input.returnPressed.connect(self._open_url)

        return box

    def _build_account_box(self):
        box = QGroupBox("账号管理")
        layout = QVBoxLayout(box)

        self._account_list = QListWidget()
        self._account_list.itemSelectionChanged.connect(self._on_account_selected)
        layout.addWidget(self._account_list)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("账号筛选平台"))
        self._account_filter_platform = QComboBox()
        self._account_filter_platform.addItem("全部", "all")
        self._account_filter_platform.addItem("转转", "zhuanzhuan")
        self._account_filter_platform.addItem("拍拍", "paipai")
        self._account_filter_platform.currentIndexChanged.connect(self._refresh_accounts)
        filter_row.addWidget(self._account_filter_platform)
        filter_row.addWidget(QLabel("分组"))
        self._account_group_filter = QLineEdit()
        self._account_group_filter.setPlaceholderText("留空表示全部")
        self._account_group_filter.textChanged.connect(self._refresh_accounts)
        filter_row.addWidget(self._account_group_filter, 1)
        layout.addLayout(filter_row)

        form = QFormLayout()
        self._acct_name = QLineEdit()
        self._acct_cookie = QPlainTextEdit()
        self._acct_cookie.setPlaceholderText("粘贴 Cookie")
        self._acct_cookie.setMinimumHeight(90)
        self._acct_note = QLineEdit()
        self._acct_platform = QComboBox()
        self._acct_platform.addItem("转转", "zhuanzhuan")
        self._acct_platform.addItem("拍拍", "paipai")
        self._acct_platform.currentIndexChanged.connect(self._on_account_platform_changed)
        self._acct_group = QLineEdit()
        self._acct_group.setPlaceholderText("例如：默认组 / A组")
        self._acct_browser_instance = QComboBox()
        self._acct_enabled = QCheckBox("启用此账号")
        self._acct_enabled.setChecked(True)
        form.addRow("账号名称", self._acct_name)
        form.addRow("平台", self._acct_platform)
        form.addRow("分组", self._acct_group)
        form.addRow("绑定浏览器", self._acct_browser_instance)
        form.addRow("Cookie", self._acct_cookie)
        form.addRow("备注", self._acct_note)
        form.addRow("", self._acct_enabled)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存/更新")
        delete_btn = QPushButton("删除")
        self._test_btn = QPushButton("测试连接")
        clear_btn = QPushButton("清空表单")
        btn_row.addWidget(save_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addWidget(self._test_btn)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

        self._account_hint = QLabel("当前阶段继续复用本地账号存储；Cookie 仍由原有服务层消费。")
        self._account_hint.setWordWrap(True)
        layout.addWidget(self._account_hint)

        self._account_log = QPlainTextEdit()
        self._account_log.setReadOnly(True)
        self._account_log.setMaximumHeight(100)
        layout.addWidget(self._account_log)

        save_btn.clicked.connect(self._save_account)
        delete_btn.clicked.connect(self._delete_account)
        self._test_btn.clicked.connect(self._test_account)
        clear_btn.clicked.connect(self._clear_account_form)

        return box

    def _build_sync_box(self):
        box = QGroupBox("成交缓存采集")
        layout = QVBoxLayout(box)

        hint = QLabel("按账号批量拉取转转已成交记录，写入本地缓存后会自动刷新筛选项、分析摘要和列表。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("同步范围"))
        self._sync_range_combo = QComboBox()
        self._sync_range_combo.addItems(list(self.SYNC_RANGE_OPTIONS.keys()))
        toolbar.addWidget(self._sync_range_combo)

        self._sync_btn = QPushButton("同步账号成交")
        clear_btn = QPushButton("删除成交缓存")
        toolbar.addWidget(self._sync_btn)
        toolbar.addWidget(clear_btn)
        toolbar.addStretch(1)

        self._cache_label = QLabel("本地缓存：0 条")
        toolbar.addWidget(self._cache_label)
        layout.addLayout(toolbar)

        self._sync_status = QLabel("采集就绪")
        self._sync_status.setWordWrap(True)
        layout.addWidget(self._sync_status)

        self._sync_log = QPlainTextEdit()
        self._sync_log.setReadOnly(True)
        self._sync_log.setMaximumHeight(120)
        layout.addWidget(self._sync_log)

        self._sync_btn.clicked.connect(self._sync_all)
        clear_btn.clicked.connect(self._clear_sold_cache)

        return box

    def _build_cache_box(self):
        box = QGroupBox("成交缓存分析与浏览")
        layout = QVBoxLayout(box)

        summary_hint = QLabel("先通过筛选缩小样本范围，再查看当前命中样本的价格区间和快速定价建议。")
        summary_hint.setWordWrap(True)
        layout.addWidget(summary_hint)

        self._summary_labels = {}
        summary_grid = QGridLayout()
        summary_specs = [
            ("sample", "样本数"),
            ("range", "样本时间范围"),
            ("avg", "平均成交价"),
            ("minmax", "参考价区间"),
            ("fast", "快速价"),
            ("cons", "保守价"),
            ("floor", "底价"),
            ("settle", "预计结算价"),
            ("confidence", "置信度"),
        ]
        for index, (key, label) in enumerate(summary_specs):
            row = index // 3
            col = (index % 3) * 2
            summary_grid.addWidget(QLabel(label), row, col)
            value_label = QLabel("—")
            value_label.setWordWrap(True)
            self._summary_labels[key] = value_label
            summary_grid.addWidget(value_label, row, col + 1)
        layout.addLayout(summary_grid)

        self._summary_warning = QLabel("调整筛选条件后会自动刷新定价摘要。")
        self._summary_warning.setWordWrap(True)
        layout.addWidget(self._summary_warning)

        filter_grid = QGridLayout()
        self._filter_boxes = {}
        filter_specs = [
            ("model", "型号"),
            ("condition", "成色"),
            ("capacity", "容量"),
            ("color", "颜色"),
        ]
        for idx, (key, label) in enumerate(filter_specs):
            row = idx // 2
            col = (idx % 2) * 2
            filter_grid.addWidget(QLabel(label), row, col)
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.lineEdit().editingFinished.connect(self._refresh_cache_filters_for_current_values)
            combo.currentTextChanged.connect(self._refresh_cache_view)
            self._filter_boxes[key] = combo
            filter_grid.addWidget(combo, row, col + 1)

        filter_grid.addWidget(QLabel("近 N 天"), 2, 0)
        self._days_combo = QComboBox()
        self._days_combo.addItems(list(self.BROWSE_RANGE_OPTIONS.keys()))
        self._days_combo.currentTextChanged.connect(self._refresh_cache_view)
        filter_grid.addWidget(self._days_combo, 2, 1)

        filter_grid.addWidget(QLabel("关键字"), 2, 2)
        self._keyword_input = QLineEdit()
        self._keyword_input.returnPressed.connect(self._refresh_cache_view)
        filter_grid.addWidget(self._keyword_input, 2, 3)
        layout.addLayout(filter_grid)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("刷新视图")
        reset_btn = QPushButton("清空筛选")
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._cache_table = QTableWidget(0, 9)
        self._cache_table.setHorizontalHeaderLabels(
            ["成交时间", "型号", "成色", "容量", "颜色", "成交价", "动销时长(h)", "来源", "标题"]
        )
        self._cache_table.setAlternatingRowColors(True)
        self._cache_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._cache_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._cache_table.verticalHeader().setVisible(False)
        header = self._cache_table.horizontalHeader()
        header.setStretchLastSection(True)
        layout.addWidget(self._cache_table, 1)

        self._cache_status = QLabel("缓存浏览就绪")
        self._cache_status.setWordWrap(True)
        layout.addWidget(self._cache_status)

        refresh_btn.clicked.connect(self._refresh_cache_view)
        reset_btn.clicked.connect(self._reset_cache_filters)

        return box

    def _current_platform(self) -> str:
        combo = getattr(self, "_platform_combo", None)
        if combo is None:
            return "zhuanzhuan"
        return str(combo.currentData() or "zhuanzhuan")

    def _platform_label(self, platform: str | None = None) -> str:
        return "拍拍" if (platform or self._current_platform()) == "paipai" else "转转"

    def _browser_instances(self):
        return self.ctx.browser_instance_store.list_all(platform=self._current_platform())

    def _ensure_default_browser_instance(self):
        self.ctx.browser_instance_store.get_default(self._current_platform(), create=True)

    def _current_browser_instance(self):
        if not self._current_instance_id:
            return None
        return self.ctx.browser_instance_store.get(self._current_platform(), self._current_instance_id)

    def _current_session(self):
        instance = self._current_browser_instance()
        if instance is None:
            return None
        return self.ctx.browser_sessions.session(instance.profile_key)

    def _browser_window_key(self, instance_id: str) -> str:
        return f"{self._current_platform()}:{instance_id}"

    def _refresh_browser_instances(self, selected_instance_id: str | None = None):
        instances = self._browser_instances()
        if not instances:
            self._current_instance_id = None
            self._browser_list.clear()
            self._sync_browser_meta()
            return
        target_id = selected_instance_id or self._current_instance_id
        if not target_id:
            default_instance = self.ctx.browser_instance_store.get_default(self._current_platform())
            target_id = default_instance.instance_id if default_instance else instances[0].instance_id

        self._browser_list.blockSignals(True)
        self._browser_list.clear()
        selected_row = 0
        for index, instance in enumerate(instances):
            label = instance.name
            if instance.is_default:
                label += "（默认）"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, instance.instance_id)
            self._browser_list.addItem(item)
            if instance.instance_id == target_id:
                selected_row = index
        self._browser_list.setCurrentRow(selected_row)
        self._browser_list.blockSignals(False)

        current_item = self._browser_list.currentItem()
        self._current_instance_id = current_item.data(Qt.ItemDataRole.UserRole) if current_item else None
        self._sync_browser_meta()

    def _on_platform_changed(self):
        self._current_instance_id = None
        self._ensure_default_browser_instance()
        self._refresh_browser_instances()
        self._set_account_platform(self._current_platform())
        self._refresh_account_browser_instances()
        self._refresh_accounts()

    def _on_browser_instance_selected(self):
        item = self._browser_list.currentItem()
        self._current_instance_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._sync_browser_meta()

    def _sync_browser_meta(self):
        instance = self._current_browser_instance()
        if instance is None:
            self._browser_name_label.setText("—")
            self._browser_id_label.setText("—")
            self._browser_profile_key_label.setText("—")
            self._browser_profile_dir_label.setText("—")
            self._browser_cookie_status_label.setText("—")
            self._browser_cookie_output.clear()
            self._browser_hint.setText("暂无浏览器实例，请先新建。")
            return
        profile_dir = self.ctx._app_ctx.browser_profile_manager.paths.profile_dir(instance.profile_key)
        name = instance.name + ("（默认）" if instance.is_default else "")
        self._browser_name_label.setText(name)
        self._browser_id_label.setText(instance.instance_id)
        self._browser_profile_key_label.setText(instance.profile_key)
        self._browser_profile_dir_label.setText(str(profile_dir))
        self._browser_cookie_status_label.setText("点击“获取 Cookie”读取当前实例的会话")
        self._browser_cookie_output.clear()
        self._browser_hint.setText("当前选中实例会以固定 profile 目录重复打开，关闭应用后登录态仍保留。")

    def _create_browser_instance(self):
        name, ok = QInputDialog.getText(self, "新建浏览器", "请输入浏览器实例名：", text="新浏览器")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "提示", "浏览器实例名不能为空")
            return
        instance = self.ctx.browser_instance_store.create(platform=self._current_platform(), name=name)
        self._refresh_browser_instances(instance.instance_id)
        self._log_account(f"新建浏览器实例：{instance.name} ({instance.profile_key})")

    def _rename_browser_instance(self):
        instance = self._current_browser_instance()
        if instance is None:
            QMessageBox.warning(self, "提示", "请先选择浏览器实例")
            return
        name, ok = QInputDialog.getText(self, "重命名浏览器", "请输入新名称：", text=instance.name)
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "提示", "浏览器实例名不能为空")
            return
        updated = self.ctx.browser_instance_store.rename(self._current_platform(), instance.instance_id, name)
        if updated is None:
            QMessageBox.warning(self, "提示", "重命名失败")
            return
        key = self._browser_window_key(updated.instance_id)
        window = self._browser_windows.get(key)
        if window is not None:
            window.setWindowTitle(f"{self._platform_label()}登录浏览器 - {updated.name}")
        self._refresh_browser_instances(updated.instance_id)

    def _delete_browser_instance(self):
        instance = self._current_browser_instance()
        if instance is None:
            QMessageBox.warning(self, "提示", "请先选择浏览器实例")
            return
        if len(self._browser_instances()) <= 1:
            QMessageBox.warning(self, "提示", "至少保留一个浏览器实例")
            return
        confirmed = QMessageBox.question(self, "删除浏览器", f"确认删除浏览器实例「{instance.name}」？\n已持久化的 profile 目录不会自动删除。")
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        key = self._browser_window_key(instance.instance_id)
        window = self._browser_windows.pop(key, None)
        if window is not None:
            window.close()
        if not self.ctx.browser_instance_store.delete(self._current_platform(), instance.instance_id):
            QMessageBox.warning(self, "提示", "删除失败")
            return
        self._refresh_browser_instances()

    def _set_default_browser_instance(self):
        instance = self._current_browser_instance()
        if instance is None:
            QMessageBox.warning(self, "提示", "请先选择浏览器实例")
            return
        updated = self.ctx.browser_instance_store.set_default(self._current_platform(), instance.instance_id)
        if updated is None:
            QMessageBox.warning(self, "提示", "设置默认失败")
            return
        self._refresh_browser_instances(updated.instance_id)

    def _format_cookie_for_header(self, cookie) -> str:
        if not isinstance(cookie, QNetworkCookie):
            return ""
        name = bytes(cookie.name()).decode("utf-8", errors="ignore").strip()
        value = bytes(cookie.value()).decode("utf-8", errors="ignore").strip()
        if not name:
            return ""
        return f"{name}={value}"

    def _collect_browser_cookies(self, session):
        profile = session.profile()
        cookie_store = profile.cookieStore()
        collector = _CookieCollector()
        loop = QEventLoop(self)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        def _restart_timer(_cookie=None):
            timer.start(200)

        collector.cookie_received.connect(_restart_timer)
        cookie_store.cookieAdded.connect(collector.on_cookie_added)
        try:
            cookie_store.loadAllCookies()
            timer.start(200)
            loop.exec()
        finally:
            try:
                collector.cookie_received.disconnect(_restart_timer)
            except (RuntimeError, TypeError):
                pass
            try:
                cookie_store.cookieAdded.disconnect(collector.on_cookie_added)
            except (RuntimeError, TypeError):
                pass
        return collector.cookies

    def _collect_browser_cookies_from_db(self, instance):
        if instance is None:
            return []
        profile_dir = self.ctx._app_ctx.browser_profile_manager.paths.profile_dir(instance.profile_key)
        cookie_db = profile_dir / "storage" / "Cookies"
        if not cookie_db.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(
                "select host_key, name, value from cookies order by host_key, name"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        pairs = []
        seen = set()
        for _host, name, value in rows:
            name = str(name or "").strip()
            value = str(value or "").strip()
            if not name:
                continue
            pair = f"{name}={value}"
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
        return pairs

    def _export_browser_cookies(self):
        instance = self._current_browser_instance()
        session = self._current_session()
        if instance is None or session is None:
            QMessageBox.warning(self, "提示", "请先选择浏览器实例")
            return
        cookies = self._collect_browser_cookies(session)
        pairs = []
        seen = set()
        for cookie in cookies:
            pair = self._format_cookie_for_header(cookie)
            if not pair or pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
        if not pairs:
            pairs = self._collect_browser_cookies_from_db(instance)
        cookie_text = "; ".join(pairs)
        self._browser_cookie_output.setPlainText(cookie_text)
        if cookie_text:
            source = "运行时会话" if seen else "持久化 Profile"
            self._browser_cookie_status_label.setText(f"已从{source}读取 {len(pairs)} 个 Cookie")
        else:
            self._browser_cookie_status_label.setText("未读取到 Cookie，请先在该实例浏览器中完成登录")

    def _apply_browser_cookies_to_account(self):
        account = self._selected_account()
        if account is None:
            QMessageBox.warning(self, "提示", "请先在账号列表中选择要回填的账号")
            return

        instance = self._current_browser_instance()
        cookie_text = self._browser_cookie_output.toPlainText().strip()
        if not cookie_text:
            self._export_browser_cookies()
            cookie_text = self._browser_cookie_output.toPlainText().strip()
        if not cookie_text:
            QMessageBox.warning(self, "提示", "当前浏览器实例没有可回填的 Cookie")
            return

        accounts = self._accounts()
        updated = False
        for index, existing in enumerate(accounts):
            if existing.name != account.name or existing.platform != account.platform:
                continue
            accounts[index] = Account(
                name=existing.name,
                cookie=cookie_text,
                note=existing.note,
                enabled=existing.enabled,
                platform=existing.platform,
                group=existing.group,
                browser_instance_id=instance.instance_id if instance is not None else existing.browser_instance_id,
            )
            updated = True
            break
        if not updated:
            QMessageBox.warning(self, "提示", f"账号「{account.name}」不存在，无法回填 Cookie")
            return

        self.ctx.account_store.save_all(accounts)
        self._acct_cookie.setPlainText(cookie_text)
        if instance is not None:
            self._refresh_account_browser_instances(instance.instance_id)
        self._refresh_accounts()
        self._sync_account_hint()
        self._log_account(f"已将浏览器实例 Cookie 回填并保存到账号：{account.name}")
        QMessageBox.information(self, "回填成功", f"已更新账号「{account.name}」的 Cookie")

    def _accounts(self):
        return self.ctx.account_store.load_all()

    def _account_platform(self) -> str:
        return str(self._acct_platform.currentData() or "zhuanzhuan")

    def _set_account_platform(self, platform: str):
        platform = (platform or "zhuanzhuan").strip() or "zhuanzhuan"
        index = self._acct_platform.findData(platform)
        self._acct_platform.setCurrentIndex(index if index >= 0 else 0)

    def _refresh_account_browser_instances(self, selected_instance_id: str | None = None):
        platform = self._account_platform()
        instances = self.ctx.browser_instance_store.list_all(platform=platform)
        current = selected_instance_id or self._acct_browser_instance.currentData()
        self._acct_browser_instance.blockSignals(True)
        self._acct_browser_instance.clear()
        self._acct_browser_instance.addItem("不绑定", "")
        for instance in instances:
            label = instance.name + ("（默认）" if instance.is_default else "")
            self._acct_browser_instance.addItem(label, instance.instance_id)
        index = self._acct_browser_instance.findData(current)
        self._acct_browser_instance.setCurrentIndex(index if index >= 0 else 0)
        self._acct_browser_instance.blockSignals(False)

    def _filtered_accounts(self):
        platform_filter = str(self._account_filter_platform.currentData() or "all")
        group_filter = self._account_group_filter.text().strip().lower()
        rows = []
        for account in self._accounts():
            if platform_filter != "all" and account.platform != platform_filter:
                continue
            if group_filter and group_filter not in (account.group or "default").lower():
                continue
            rows.append(account)
        return rows

    def _selected_account(self):
        item = self._account_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _refresh_accounts(self):
        selected_name = self._acct_name.text().strip()
        selected_platform = self._account_platform()
        self._account_list.blockSignals(True)
        self._account_list.clear()
        selected_row = -1
        rows = self._filtered_accounts()
        for index, account in enumerate(rows):
            prefix = "✅" if account.enabled else "⬜"
            group = account.group or "default"
            label = f"{prefix} [{self._platform_label(account.platform)}][{group}] {account.name}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, account)
            self._account_list.addItem(item)
            if selected_name and account.name == selected_name and account.platform == selected_platform:
                selected_row = index
        if selected_row >= 0:
            self._account_list.setCurrentRow(selected_row)
        self._account_list.blockSignals(False)
        self._sync_account_hint()

    def _on_account_platform_changed(self):
        self._refresh_account_browser_instances()

    def _on_account_selected(self):
        account = self._selected_account()
        if account is None:
            self._sync_account_hint()
            return
        self._acct_name.setText(account.name)
        self._set_account_platform(account.platform)
        self._acct_group.setText(account.group or "default")
        self._refresh_account_browser_instances(account.browser_instance_id)
        self._acct_cookie.setPlainText(account.cookie)
        self._acct_note.setText(account.note)
        self._acct_enabled.setChecked(account.enabled)
        self._sync_account_hint()

    def _sync_account_hint(self):
        account = self._selected_account()
        if account is None:
            self._account_hint.setText("当前阶段继续复用本地账号存储；Cookie 仍由原有服务层消费。")
            return
        note = account.note or "无备注"
        status = "启用" if account.enabled else "停用"
        group = account.group or "default"
        browser_instance = account.browser_instance_id or "未绑定"
        self._account_hint.setText(
            f"账号：{account.name}\n平台：{self._platform_label(account.platform)}\n分组：{group}\n"
            f"状态：{status}\n绑定浏览器：{browser_instance}\n备注：{note}"
        )

    def _clear_account_form(self):
        self._account_list.clearSelection()
        self._acct_name.clear()
        self._set_account_platform(self._current_platform())
        self._acct_group.setText("default")
        self._refresh_account_browser_instances()
        self._acct_cookie.clear()
        self._acct_note.clear()
        self._acct_enabled.setChecked(True)
        self._sync_account_hint()

    def _save_account(self):
        name = self._acct_name.text().strip()
        cookie = self._acct_cookie.toPlainText().strip()
        if not name or not cookie:
            QMessageBox.warning(self, "提示", "账号名和 Cookie 不能为空")
            return

        platform = self._account_platform()
        group = self._acct_group.text().strip() or "default"
        browser_instance_id = str(self._acct_browser_instance.currentData() or "")
        accounts = self._accounts()
        existing_index = -1
        for index, account in enumerate(accounts):
            if account.name == name and account.platform == platform:
                existing_index = index
                break

        new_account = Account(
            name=name,
            cookie=cookie,
            note=self._acct_note.text().strip(),
            enabled=self._acct_enabled.isChecked(),
            platform=platform,
            group=group,
            browser_instance_id=browser_instance_id,
        )
        if existing_index >= 0:
            accounts[existing_index] = new_account
            action = "更新"
        else:
            accounts.append(new_account)
            action = "保存"
        self.ctx.account_store.save_all(accounts)
        self._refresh_accounts()
        self._log_account(f"{action}账号：{name}（{self._platform_label(platform)} / {group}）")
        QMessageBox.information(self, action, f"账号「{name}」已{action}")

    def _delete_account(self):
        account = self._selected_account()
        if account is None:
            return
        accounts = self._accounts()
        target_index = -1
        for index, existing in enumerate(accounts):
            if existing.name == account.name and existing.platform == account.platform:
                target_index = index
                break
        if target_index < 0:
            return
        name = account.name
        confirmed = QMessageBox.question(self, "删除", f"确认删除账号「{name}」？")
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        del accounts[target_index]
        self.ctx.account_store.save_all(accounts)
        self._clear_account_form()
        self._refresh_accounts()
        self._log_account(f"删除账号：{name}")

    def _test_account(self):
        name = self._acct_name.text().strip()
        cookie = self._acct_cookie.toPlainText().strip()
        if not name or not cookie:
            QMessageBox.warning(self, "提示", "请先填写账号信息")
            return
        if self._account_test_in_progress:
            return

        platform = self._account_platform()
        self._account_test_in_progress = True
        self._account_test_result = None
        self._test_btn.setEnabled(False)
        self._log_account(f"测试连接中 …（{self._platform_label(platform)}）")

        def _worker():
            try:
                if platform == "paipai":
                    cfg = PaipaiConfig()
                    ok = bool(cookie)
                    message = "Cookie 已填写"
                    if not ok:
                        message = "未填写拍拍 Cookie"
                    elif not cfg.sign:
                        message = "未配置拍拍 sign（可先保存 Cookie，后续补齐）"
                    elif not cfg.body:
                        message = "未配置拍拍 body（可先保存 Cookie，后续补齐）"
                    else:
                        message = "拍拍基础配置可用"
                    self._account_test_result = (ok, message)
                    return
                service = ImeiService(name, cookie)
                ok, message = service.check_cookie_valid()
                self._account_test_result = (ok, message or ("连接成功" if ok else "连接失败"))
            except Exception as exc:
                self._account_test_result = (False, f"连接失败: {exc}")

        threading.Thread(target=_worker, daemon=True).start()
        QTimer.singleShot(200, self._poll_account_test_result)

    def _poll_account_test_result(self):
        if self._account_test_result is None:
            QTimer.singleShot(200, self._poll_account_test_result)
            return
        ok, message = self._account_test_result
        self._account_test_result = None
        self._account_test_in_progress = False
        self._test_btn.setEnabled(True)
        self._log_account(("✓ " if ok else "✗ ") + message)
        if ok:
            QMessageBox.information(self, "测试连接", message)
        else:
            QMessageBox.critical(self, "测试连接失败", message)

    def _sync_all(self):
        if self._sync_in_progress:
            return
        accounts = self.ctx.account_store.enabled_accounts()
        if not accounts:
            QMessageBox.warning(self, "提示", "无可用账号")
            return

        range_label = self._sync_range_combo.currentText()
        days = self.SYNC_RANGE_OPTIONS.get(range_label, 0)
        since = datetime.now() - timedelta(days=days) if days else None
        self._sync_in_progress = True
        self._sync_result = None
        self._sync_btn.setEnabled(False)
        self._sync_status.setText(f"开始同步，共 {len(accounts)} 个启用账号")
        self._log_sync(f"开始同步账号成交（范围：{range_label}）")

        threading.Thread(target=self._do_sync_all, args=(accounts, since, range_label), daemon=True).start()
        QTimer.singleShot(200, self._poll_sync_result)

    def _do_sync_all(self, accounts, since=None, range_label="全部"):
        total_added = 0
        total_updated = 0
        logs = []
        for account in accounts:
            logs.append(f"[{account.name}] 拉取成交数据（{range_label}）…")
            try:
                fetcher = DataFetcher(account.name, account.cookie)
                records = fetcher.fetch_all_sold(since=since)
                added, updated = self.ctx.sold_cache.upsert(records)
                total_added += added
                total_updated += updated
                logs.append(f"[{account.name}] 新增 {added} 条，更新 {updated} 条")
            except Exception as exc:
                logs.append(f"[{account.name}] 失败: {exc}")
        self._sync_result = {
            "added": total_added,
            "updated": total_updated,
            "logs": logs,
        }

    def _poll_sync_result(self):
        if self._sync_result is None:
            QTimer.singleShot(200, self._poll_sync_result)
            return
        result = self._sync_result
        self._sync_result = None
        self._sync_in_progress = False
        self._sync_btn.setEnabled(True)
        for line in result.get("logs", []):
            self._log_sync(line)
        self._after_sync_done(result.get("added", 0), result.get("updated", 0))

    def _after_sync_done(self, total_added: int, total_updated: int = 0):
        summary = f"同步完成，共新增 {total_added} 条"
        if total_updated:
            summary += f"，更新 {total_updated} 条"
        self._sync_status.setText(summary)
        self._log_sync(summary)
        self._refresh_cache_filters()
        self._refresh_cache_view()

    def _clear_sold_cache(self):
        count = self.ctx.sold_cache.count()
        if count <= 0:
            QMessageBox.information(self, "删除成交缓存", "当前没有可删除的成交缓存")
            return
        confirmed = QMessageBox.question(
            self,
            "删除成交缓存",
            f"确认删除本地 {count} 条成交缓存？删除后可重新同步。",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self.ctx.sold_cache.clear()
        self._log_sync(f"已删除本地成交缓存 {count} 条")
        self._sync_status.setText(f"已清空本地成交缓存（原有 {count} 条）")
        self._refresh_cache_filters()
        self._refresh_cache_view()

    def _refresh_cache_filters(self):
        current_values = self._current_filter_values()
        options = self.ctx.sold_cache.get_filter_options(
            model=current_values["model"],
            condition=current_values["condition"],
            capacity=current_values["capacity"],
            color=current_values["color"],
            fuzzy_model=True,
        )
        for key, combo in self._filter_boxes.items():
            value = current_values[key]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("")
            combo.addItems(options.get(key, []))
            combo.setCurrentText(value)
            combo.blockSignals(False)

    def _refresh_cache_filters_for_current_values(self):
        self._refresh_cache_filters()
        self._refresh_cache_view()

    def _current_filter_values(self):
        return {key: combo.currentText().strip() for key, combo in self._filter_boxes.items()}

    def _reset_cache_filters(self):
        for combo in self._filter_boxes.values():
            combo.blockSignals(True)
            combo.setCurrentText("")
            combo.blockSignals(False)
        self._days_combo.setCurrentText("全部")
        self._keyword_input.clear()
        self._refresh_cache_filters()
        self._refresh_cache_view()

    def _query_cache_records(self):
        values = self._current_filter_values()
        days = self.BROWSE_RANGE_OPTIONS.get(self._days_combo.currentText(), 0)
        return self.ctx.sold_cache.query_records(
            model=values["model"],
            condition=values["condition"],
            capacity=values["capacity"],
            color=values["color"],
            days=days,
            keyword=self._keyword_input.text().strip(),
            limit=1000,
            fuzzy_model=True,
        )

    def _refresh_cache_view(self):
        records = self._query_cache_records()
        self._refresh_cache_table(records)
        self._refresh_cache_summary(records)

    def _refresh_cache_table(self, records):
        self._cache_table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                record.sold_time.strftime("%m-%d %H:%M"),
                record.model[:20],
                record.condition[:14],
                record.capacity[:10],
                record.color[:10],
                f"¥{record.sold_price:,.0f}",
                f"{record.hours_to_sell:.1f}" if record.hours_to_sell is not None else "—",
                record.source,
                record.title[:60],
            ]
            for column, value in enumerate(values):
                self._cache_table.setItem(row, column, QTableWidgetItem(value))

        total_count = self.ctx.sold_cache.count()
        self._cache_label.setText(f"本地缓存：{total_count} 条")
        self._cache_status.setText(f"当前显示 {len(records)} 条记录")

    def _refresh_cache_summary(self, records):
        total = len(records)
        summary = self._summary_labels
        if not records:
            summary["sample"].setText("0")
            summary["range"].setText("—")
            summary["avg"].setText("—")
            summary["minmax"].setText("—")
            summary["fast"].setText("—")
            summary["cons"].setText("—")
            summary["floor"].setText("—")
            summary["settle"].setText("—")
            summary["confidence"].setText("无数据")
            self._summary_warning.setText("当前筛选下暂无成交样本，请调整筛选条件或先同步缓存。")
            return

        filter_values = self._current_filter_values()
        prices = [record.sold_price for record in records]
        earliest = min(record.sold_time for record in records)
        latest = max(record.sold_time for record in records)
        pricing = self.ctx.pricing_engine.calculate(
            records,
            filter_values["model"],
            filter_values["condition"],
            filter_values["capacity"],
            filter_values["color"],
        )

        summary["sample"].setText(str(total))
        summary["range"].setText(f"{earliest.strftime('%Y-%m-%d')} ~ {latest.strftime('%Y-%m-%d')}")
        summary["avg"].setText(f"¥{(sum(prices) / total):,.0f}")
        summary["minmax"].setText(f"¥{min(prices):,.0f} ~ ¥{max(prices):,.0f}")
        summary["fast"].setText(self._format_price(pricing.fast_price))
        summary["cons"].setText(self._format_price(pricing.conservative_price))
        summary["floor"].setText(self._format_price(pricing.floor_price))
        summary["settle"].setText(self._format_price(pricing.settle_price))
        summary["confidence"].setText(self._format_confidence(pricing.confidence))
        self._summary_warning.setText(pricing.warning or "样本充足，可直接参考快速价 / 保守价 / 底价区间。")

    def _format_price(self, value):
        if value is None:
            return "—"
        return f"¥{value:,.0f}"

    def _format_confidence(self, value):
        mapping = {
            ConfidenceLevel.HIGH: "高",
            ConfidenceLevel.LOW: "低",
            ConfidenceLevel.NONE: "无数据",
            "high": "高",
            "low": "低",
            "none": "无数据",
        }
        return mapping.get(value, str(value or "—"))

    def _ensure_browser_window(self):
        instance = self._current_browser_instance()
        session = self._current_session()
        if instance is None or session is None:
            QMessageBox.warning(self, "提示", "请先选择浏览器实例")
            return None
        platform_label = self._platform_label()
        key = self._browser_window_key(instance.instance_id)
        if key not in self._browser_windows:
            self._browser_windows[key] = FetchBrowserWindow(
                session,
                window_title=f"{platform_label}登录浏览器 - {instance.name}",
                pane_title=f"{platform_label}数据管理 / 登录会话 / {instance.name}",
            )
        return self._browser_windows[key]

    def _show_browser_window(self):
        window = self._ensure_browser_window()
        if window is None:
            return None
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def _browser_pane(self):
        window = self._show_browser_window()
        return window.browser if window is not None else None

    def _open_url(self):
        browser = self._browser_pane()
        if browser is None:
            return
        url = self._url_input.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
            self._url_input.setText(url)
        browser.view.load(QUrl(url))

    def _reload_browser(self):
        browser = self._browser_pane()
        if browser is not None:
            browser.view.reload()

    def _log_account(self, message: str):
        self._account_log.appendPlainText(message)

    def _log_sync(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._sync_log.appendPlainText(f"[{timestamp}] {message}")
