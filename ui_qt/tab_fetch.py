# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Account, ConfidenceLevel
from ..services.zhuanzhuan_api import DataFetcher, ImeiService
from .browser_pane import BrowserPane


class FetchBrowserWindow(QMainWindow):
    def __init__(self, session):
        super().__init__()
        self.setWindowTitle("转转登录浏览器")
        self.resize(1180, 860)
        self.browser = BrowserPane(
            session,
            title="转转数据管理 / 登录会话",
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
        self.session = ctx.browser_sessions.fetch
        self._browser_window = None
        self._account_test_in_progress = False
        self._account_test_result = None
        self._sync_in_progress = False
        self._sync_result = None
        self._build_ui()
        self._refresh_accounts()
        self._refresh_cache_filters()
        self._refresh_cache_view()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "该页面用于管理转转登录会话、账号 Cookie、本地成交缓存采集与基础定价分析。浏览器仍保持独立窗口模式，需要登录时再打开。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(self._build_sidebar(), 1)

    def _build_sidebar(self):
        sidebar = QWidget()
        layout = QVBoxLayout(sidebar)

        layout.addWidget(self._build_profile_box())
        layout.addWidget(self._build_account_box())
        layout.addWidget(self._build_sync_box())
        layout.addWidget(self._build_cache_box(), 1)

        return sidebar

    def _build_profile_box(self):
        box = QGroupBox("浏览器会话")
        layout = QVBoxLayout(box)

        layout.addWidget(QLabel(f"Profile Key: {self.session.profile_key}"))
        profile_dir = self.ctx._app_ctx.browser_profile_manager.paths.profile_dir(self.session.profile_key)
        profile_label = QLabel(f"Profile Dir: {profile_dir}")
        profile_label.setWordWrap(True)
        layout.addWidget(profile_label)

        browser_row = QHBoxLayout()
        open_browser_btn = QPushButton("打开浏览器")
        browser_row.addWidget(open_browser_btn)
        browser_row.addStretch(1)
        layout.addLayout(browser_row)

        nav_row = QHBoxLayout()
        self._url_input = QLineEdit("https://www.zhuanzhuan.com/")
        go_btn = QPushButton("打开")
        reload_btn = QPushButton("刷新")
        nav_row.addWidget(self._url_input, 1)
        nav_row.addWidget(go_btn)
        nav_row.addWidget(reload_btn)
        layout.addLayout(nav_row)

        hint = QLabel("该 tab 使用独立 QWebEngineProfile；浏览器窗口可反复打开/聚焦，Cookie / LocalStorage / Session 会单独持久化。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        open_browser_btn.clicked.connect(self._show_browser_window)
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

        form = QFormLayout()
        self._acct_name = QLineEdit()
        self._acct_cookie = QPlainTextEdit()
        self._acct_cookie.setPlaceholderText("粘贴 Cookie")
        self._acct_cookie.setMinimumHeight(90)
        self._acct_note = QLineEdit()
        self._acct_enabled = QCheckBox("启用此账号")
        self._acct_enabled.setChecked(True)
        form.addRow("账号名称", self._acct_name)
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

    def _accounts(self):
        return self.ctx.account_store.load_all()

    def _selected_account(self):
        row = self._account_list.currentRow()
        accounts = self._accounts()
        if row < 0 or row >= len(accounts):
            return None
        return accounts[row]

    def _refresh_accounts(self):
        selected_name = self._acct_name.text().strip()
        self._account_list.clear()
        selected_row = -1
        for index, account in enumerate(self._accounts()):
            prefix = "✅" if account.enabled else "⬜"
            self._account_list.addItem(f"{prefix} {account.name}")
            if selected_name and account.name == selected_name:
                selected_row = index
        if selected_row >= 0:
            self._account_list.setCurrentRow(selected_row)
        self._sync_account_hint()

    def _on_account_selected(self):
        account = self._selected_account()
        if account is None:
            self._sync_account_hint()
            return
        self._acct_name.setText(account.name)
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
        self._account_hint.setText(f"账号：{account.name}\n状态：{status}\n备注：{note}")

    def _clear_account_form(self):
        self._account_list.clearSelection()
        self._acct_name.clear()
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

        accounts = self._accounts()
        existing = {account.name: index for index, account in enumerate(accounts)}
        new_account = Account(
            name=name,
            cookie=cookie,
            note=self._acct_note.text().strip(),
            enabled=self._acct_enabled.isChecked(),
        )
        if name in existing:
            accounts[existing[name]] = new_account
            action = "更新"
        else:
            accounts.append(new_account)
            action = "保存"
        self.ctx.account_store.save_all(accounts)
        self._refresh_accounts()
        self._log_account(f"{action}账号：{name}")
        QMessageBox.information(self, action, f"账号「{name}」已{action}")

    def _delete_account(self):
        row = self._account_list.currentRow()
        accounts = self._accounts()
        if row < 0 or row >= len(accounts):
            return
        name = accounts[row].name
        confirmed = QMessageBox.question(self, "删除", f"确认删除账号「{name}」？")
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        del accounts[row]
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

        self._account_test_in_progress = True
        self._account_test_result = None
        self._test_btn.setEnabled(False)
        self._log_account("测试连接中 …")

        def _worker():
            try:
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
        if self._browser_window is None:
            self._browser_window = FetchBrowserWindow(self.session)
        return self._browser_window

    def _show_browser_window(self):
        window = self._ensure_browser_window()
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def _browser_pane(self):
        return self._show_browser_window().browser

    def _open_url(self):
        url = self._url_input.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
            self._url_input.setText(url)
        self._browser_pane().view.load(QUrl(url))

    def _reload_browser(self):
        self._browser_pane().view.reload()

    def _log_account(self, message: str):
        self._account_log.appendPlainText(message)

    def _log_sync(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._sync_log.appendPlainText(f"[{timestamp}] {message}")
