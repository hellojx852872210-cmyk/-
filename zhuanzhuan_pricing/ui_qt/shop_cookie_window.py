# -*- coding: utf-8 -*-
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
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
    QVBoxLayout,
    QWidget,
)

from ..services.shop_cookie_service import ShopCookieService


class LoginBrowserWindow(QMainWindow):
    def __init__(self, view, *, title: str):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(1180, 860)
        self.setCentralWidget(view)

    def closeEvent(self, event: QCloseEvent):
        self.hide()
        event.ignore()


class ShopCookieWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.service = ShopCookieService()
        self._browser_windows: dict[str, LoginBrowserWindow] = {}
        self._selected_instance_id: str | None = None
        self._build_ui()
        self._ensure_default_instance()
        self._refresh_instances()
        self._refresh_account_instance_combo()
        self._refresh_accounts()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        title = QLabel("Cookie 获取与账号管理（MVP）")
        title.setWordWrap(True)
        layout.addWidget(title)

        top = QHBoxLayout()
        top.addWidget(QLabel("平台"))
        self._platform = QComboBox()
        self._platform.addItem("转转", "zhuanzhuan")
        self._platform.addItem("拍拍", "paipai")
        self._platform.currentIndexChanged.connect(self._on_platform_changed)
        top.addWidget(self._platform)
        top.addStretch(1)
        layout.addLayout(top)

        layout.addWidget(self._build_instance_box())
        layout.addWidget(self._build_account_box())

    def _build_instance_box(self):
        box = QGroupBox("隔离浏览器实例")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        self._instance_list = QListWidget()
        self._instance_list.itemSelectionChanged.connect(self._on_instance_selected)
        row.addWidget(self._instance_list, 2)

        right = QVBoxLayout()
        self._instance_meta = QLabel("—")
        self._instance_meta.setWordWrap(True)
        right.addWidget(self._instance_meta)

        nav = QHBoxLayout()
        self._url = QLineEdit("https://b.zhuanzhuan.com/")
        go_btn = QPushButton("打开")
        reload_btn = QPushButton("刷新")
        nav.addWidget(self._url, 1)
        nav.addWidget(go_btn)
        nav.addWidget(reload_btn)
        right.addLayout(nav)

        self._cookie_output = QPlainTextEdit()
        self._cookie_output.setPlaceholderText("点击‘获取 Cookie’后显示")
        self._cookie_output.setMaximumHeight(100)
        right.addWidget(self._cookie_output)

        btns = QGridLayout()
        new_btn = QPushButton("新建")
        default_btn = QPushButton("设默认")
        open_btn = QPushButton("打开浏览器")
        export_btn = QPushButton("获取 Cookie")
        btns.addWidget(new_btn, 0, 0)
        btns.addWidget(default_btn, 0, 1)
        btns.addWidget(open_btn, 1, 0)
        btns.addWidget(export_btn, 1, 1)
        right.addLayout(btns)
        row.addLayout(right, 3)

        layout.addLayout(row)

        new_btn.clicked.connect(self._create_instance)
        default_btn.clicked.connect(self._set_default_instance)
        open_btn.clicked.connect(self._open_browser)
        export_btn.clicked.connect(self._export_cookie)
        go_btn.clicked.connect(self._open_url)
        reload_btn.clicked.connect(self._reload_browser)
        self._url.returnPressed.connect(self._open_url)
        return box

    def _build_account_box(self):
        box = QGroupBox("账号管理")
        layout = QVBoxLayout(box)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("分组筛选"))
        self._group_filter = QLineEdit()
        self._group_filter.setPlaceholderText("留空显示全部")
        self._group_filter.textChanged.connect(self._refresh_accounts)
        filter_row.addWidget(self._group_filter)
        layout.addLayout(filter_row)

        self._account_list = QListWidget()
        self._account_list.itemSelectionChanged.connect(self._on_account_selected)
        layout.addWidget(self._account_list)

        form = QFormLayout()
        self._acct_name = QLineEdit()
        self._acct_group = QLineEdit("default")
        self._acct_instance = QComboBox()
        self._acct_cookie = QPlainTextEdit()
        self._acct_cookie.setMinimumHeight(90)
        self._acct_note = QLineEdit()
        self._acct_enabled = QCheckBox("启用")
        self._acct_enabled.setChecked(True)
        form.addRow("账号名", self._acct_name)
        form.addRow("分组", self._acct_group)
        form.addRow("绑定实例", self._acct_instance)
        form.addRow("Cookie", self._acct_cookie)
        form.addRow("备注", self._acct_note)
        form.addRow("", self._acct_enabled)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存/更新")
        apply_btn = QPushButton("实例Cookie回填")
        test_btn = QPushButton("测试连接")
        clear_btn = QPushButton("清空")
        btn_row.addWidget(save_btn)
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(test_btn)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

        self._status = QLabel("就绪")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        save_btn.clicked.connect(self._save_account)
        apply_btn.clicked.connect(self._apply_cookie)
        test_btn.clicked.connect(self._validate_account)
        clear_btn.clicked.connect(self._clear_account)
        return box

    def _current_platform(self) -> str:
        return str(self._platform.currentData() or "zhuanzhuan")

    def _on_platform_changed(self):
        self._ensure_default_instance()
        self._refresh_instances()
        self._refresh_account_instance_combo()
        self._refresh_accounts()

    def _ensure_default_instance(self):
        self.service.get_or_create_default_instance(self._current_platform())

    def _refresh_instances(self):
        instances = self.service.list_instances(self._current_platform())
        self._instance_list.blockSignals(True)
        self._instance_list.clear()
        for idx, ins in enumerate(instances):
            label = ins.name + ("（默认）" if ins.is_default else "")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, ins.instance_id)
            self._instance_list.addItem(item)
            if idx == 0:
                self._instance_list.setCurrentRow(0)
        self._instance_list.blockSignals(False)
        self._on_instance_selected()

    def _current_instance_id(self) -> str | None:
        item = self._instance_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_instance_selected(self):
        instance_id = self._current_instance_id()
        self._selected_instance_id = instance_id
        if not instance_id:
            self._instance_meta.setText("—")
            return
        instance = self.service.get_instance(self._current_platform(), instance_id)
        if instance is None:
            self._instance_meta.setText("—")
            return
        self._instance_meta.setText(f"实例: {instance.name}\nID: {instance.instance_id}\nProfile: {instance.profile_key}")

    def _create_instance(self):
        name, ok = QInputDialog.getText(self, "新建实例", "实例名", text="新浏览器")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "提示", "实例名不能为空")
            return
        self.service.create_instance(self._current_platform(), name)
        self._refresh_instances()
        self._refresh_account_instance_combo()

    def _set_default_instance(self):
        instance_id = self._current_instance_id()
        if not instance_id:
            return
        self.service.set_default_instance(self._current_platform(), instance_id)
        self._refresh_instances()
        self._refresh_account_instance_combo()

    def _ensure_browser_window(self):
        instance_id = self._current_instance_id()
        if not instance_id:
            QMessageBox.warning(self, "提示", "请先选择实例")
            return None
        key = f"{self._current_platform()}:{instance_id}"
        if key in self._browser_windows:
            return self._browser_windows[key]
        session = self.service.session_for_instance(self._current_platform(), instance_id)
        view = session.create_view()
        view.load(QUrl(self._url.text().strip() or "https://b.zhuanzhuan.com/"))
        window = LoginBrowserWindow(view, title=f"{self.service.platform_label(self._current_platform())} 登录浏览器")
        self._browser_windows[key] = window
        return window

    def _open_browser(self):
        window = self._ensure_browser_window()
        if window is None:
            return
        window.show()
        window.raise_()
        window.activateWindow()

    def _open_url(self):
        window = self._ensure_browser_window()
        if window is None:
            return
        url = self._url.text().strip()
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
            self._url.setText(url)
        window.centralWidget().load(QUrl(url))

    def _reload_browser(self):
        window = self._ensure_browser_window()
        if window is None:
            return
        window.centralWidget().reload()

    def _export_cookie(self):
        instance_id = self._current_instance_id()
        if not instance_id:
            QMessageBox.warning(self, "提示", "请先选择实例")
            return
        cookie_text, source = self.service.export_cookie(self._current_platform(), instance_id)
        self._cookie_output.setPlainText(cookie_text)
        if cookie_text:
            self._status.setText(f"已读取 Cookie（{source}）")
        else:
            self._status.setText("未读取到 Cookie，请先登录")

    def _refresh_account_instance_combo(self, selected: str | None = None):
        instances = self.service.list_instances(self._current_platform())
        current = selected or self._acct_instance.currentData()
        self._acct_instance.clear()
        self._acct_instance.addItem("不绑定", "")
        for ins in instances:
            self._acct_instance.addItem(ins.name, ins.instance_id)
        idx = self._acct_instance.findData(current)
        self._acct_instance.setCurrentIndex(idx if idx >= 0 else 0)

    def _refresh_accounts(self):
        current = self._selected_account()
        current_key = None
        if current is not None:
            current_key = (current.name, current.platform)

        rows = self.service.list_accounts(platform=self._current_platform(), group=(self._group_filter.text().strip() or None))
        self._account_list.blockSignals(True)
        self._account_list.clear()
        selected_row = -1
        for idx, acc in enumerate(rows):
            prefix = "✅" if acc.enabled else "⬜"
            item = QListWidgetItem(f"{prefix} [{acc.group}] {acc.name}")
            item.setData(Qt.ItemDataRole.UserRole, acc)
            self._account_list.addItem(item)
            if current_key and (acc.name, acc.platform) == current_key:
                selected_row = idx
        self._account_list.blockSignals(False)
        if selected_row >= 0:
            self._account_list.setCurrentRow(selected_row)

    def _selected_account(self):
        item = self._account_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_account_selected(self):
        acc = self._selected_account()
        if acc is None:
            return
        self._acct_name.setText(acc.name)
        self._acct_group.setText(acc.group or "default")
        self._refresh_account_instance_combo(acc.browser_instance_id)
        self._acct_cookie.setPlainText(acc.cookie)
        self._acct_note.setText(acc.note)
        self._acct_enabled.setChecked(acc.enabled)

    def _save_account(self):
        try:
            acc = self.service.save_account(
                name=self._acct_name.text().strip(),
                cookie=self._acct_cookie.toPlainText().strip(),
                platform=self._current_platform(),
                group=self._acct_group.text().strip() or "default",
                browser_instance_id=str(self._acct_instance.currentData() or ""),
                note=self._acct_note.text().strip(),
                enabled=self._acct_enabled.isChecked(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._status.setText(f"已保存账号：{acc.name}")
        self._refresh_accounts()
        self._refresh_account_instance_combo(acc.browser_instance_id)

    def _apply_cookie(self):
        name = self._acct_name.text().strip()
        instance_id = str(self._acct_instance.currentData() or self._selected_instance_id or "")
        if not name or not instance_id:
            QMessageBox.warning(self, "提示", "请先填写账号名并选择实例")
            return
        try:
            acc = self.service.apply_cookie_from_instance(
                name=name,
                platform=self._current_platform(),
                instance_id=instance_id,
                group=self._acct_group.text().strip() or None,
            )
        except Exception as exc:
            QMessageBox.critical(self, "回填失败", str(exc))
            return
        self._acct_cookie.setPlainText(acc.cookie)
        self._status.setText(f"已回填实例 Cookie 到账号：{acc.name}")
        self._refresh_accounts()
        self._refresh_account_instance_combo(acc.browser_instance_id)

    def _validate_account(self):
        name = self._acct_name.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请先填写账号名")
            return
        ok, message = self.service.validate_account(name=name, platform=self._current_platform(), cookie=self._acct_cookie.toPlainText().strip())
        self._status.setText(("✓ " if ok else "✗ ") + message)
        if ok:
            QMessageBox.information(self, "校验结果", message)
        else:
            QMessageBox.warning(self, "校验结果", message)

    def _clear_account(self):
        self._acct_name.clear()
        self._acct_group.setText("default")
        self._refresh_account_instance_combo()
        self._acct_cookie.clear()
        self._acct_note.clear()
        self._acct_enabled.setChecked(True)


class ShopCookieMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("店铺 Cookie 管理")
        self.resize(1240, 860)
        self.setCentralWidget(ShopCookieWindow())
