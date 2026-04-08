# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
import threading
from collections import defaultdict

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import BatchItem, ConfidenceLevel, PriceChangeRecord, PriceTrigger
from ..core.pricing_engine import PricingEngine
from ..services.zhuanzhuan_api import ImeiService


class ZhuanzhuanBatchQtTab(QWidget):
    PAGE_SIZE = 50

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._current_page = 1
        self._fetch_in_progress = False
        self._fetch_result = None
        self._match_in_progress = False
        self._match_result = None
        self._reprice_in_progress = False
        self._reprice_result = None
        self._build_ui()
        self._refresh_view()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel("该页面用于批量拉取在售商品、基于本地成交缓存匹配建议价，并批量执行改价。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_toolbar_box())
        layout.addWidget(self._build_summary_box())
        layout.addWidget(self._build_table_box(), 1)
        layout.addWidget(self._build_pager_box())
        layout.addWidget(self._build_log_box())

    def _build_toolbar_box(self):
        box = QGroupBox("批量动作")
        layout = QHBoxLayout(box)

        self._fetch_btn = QPushButton("拉取在售商品")
        self._match_btn = QPushButton("匹配建议价")
        self._reprice_btn = QPushButton("批量改价")
        self._clear_btn = QPushButton("清空列表")
        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("筛选标题 / 质检码 / 型号")

        layout.addWidget(self._fetch_btn)
        layout.addWidget(self._match_btn)
        layout.addWidget(self._reprice_btn)
        layout.addWidget(self._clear_btn)
        layout.addWidget(QLabel("筛选"))
        layout.addWidget(self._filter_input, 1)

        self._status_label = QLabel("就绪")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._fetch_btn.clicked.connect(self._fetch_products)
        self._match_btn.clicked.connect(self._match_prices)
        self._reprice_btn.clicked.connect(self._batch_reprice)
        self._clear_btn.clicked.connect(self._clear_list)
        self._filter_input.textChanged.connect(self._on_filter_changed)

        return box

    def _build_summary_box(self):
        box = QGroupBox("当前批次摘要")
        layout = QGridLayout(box)

        self._summary_labels = {}
        specs = [
            ("total", "商品总数"),
            ("matched", "已匹配"),
            ("pending", "待改价"),
            ("success", "改价成功"),
            ("failed", "改价失败"),
            ("page", "当前页"),
        ]
        for index, (key, label) in enumerate(specs):
            row = index // 3
            col = (index % 3) * 2
            layout.addWidget(QLabel(label), row, col)
            value_label = QLabel("0")
            value_label.setWordWrap(True)
            self._summary_labels[key] = value_label
            layout.addWidget(value_label, row, col + 1)

        return box

    def _build_table_box(self):
        box = QGroupBox("商品列表")
        layout = QVBoxLayout(box)

        self._table = QTableWidget(0, 12)
        self._table.setHorizontalHeaderLabels([
            "质检码",
            "商品名",
            "成色",
            "容量",
            "当前价",
            "建议价",
            "差价",
            "预计到手价",
            "样本数",
            "置信度",
            "状态",
            "账号",
        ])
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        return box

    def _build_pager_box(self):
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        self._prev_btn = QPushButton("上一页")
        self._next_btn = QPushButton("下一页")
        self._page_label = QLabel("第 1/1 页")
        self._total_label = QLabel("共 0 条")

        layout.addWidget(self._prev_btn)
        layout.addWidget(self._next_btn)
        layout.addWidget(self._page_label)
        layout.addWidget(self._total_label)
        layout.addStretch(1)

        self._prev_btn.clicked.connect(self._prev_page)
        self._next_btn.clicked.connect(self._next_page)
        return box

    def _build_log_box(self):
        box = QGroupBox("执行日志")
        layout = QVBoxLayout(box)

        self._log_text = QPlainTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumHeight(120)
        layout.addWidget(self._log_text)

        return box

    def _fetch_products(self):
        if self._fetch_in_progress:
            return
        accounts = self.ctx.account_store.enabled_accounts()
        if not accounts:
            QMessageBox.warning(self, "提示", "请先在“数据管理”添加启用账号")
            return

        self._fetch_in_progress = True
        self._fetch_result = None
        self._set_busy("fetch", True)
        self._status_label.setText("正在拉取在售商品…")
        self._log(f"开始拉取在售商品，共 {len(accounts)} 个账号")
        threading.Thread(target=self._do_fetch, args=(accounts,), daemon=True).start()
        QTimer.singleShot(150, self._poll_fetch_result)

    def _do_fetch(self, accounts):
        all_items = []
        logs = []
        for account in accounts:
            try:
                service = ImeiService(account.name, account.cookie)
                products = service.fetch_all_on_sale()
                logs.append(f"[{account.name}] 拉取 {len(products)} 件在售商品")
                for product in products:
                    item = BatchItem(
                        product_id=product.product_id,
                        qc_code=product.qc_code,
                        title=product.title,
                        current_price=product.current_price,
                        status=product.status,
                        account_name=account.name,
                        imei=product.imei,
                        model=product.model,
                        condition=product.condition,
                        capacity=product.capacity,
                        color=product.color,
                        listed_time=product.listed_time,
                    )
                    item.cost_price = self.ctx.cost_map.get(product.product_id)
                    all_items.append(item)
            except Exception as exc:
                logs.append(f"[{account.name}] 拉取失败: {exc}")
        self._fetch_result = {"items": all_items, "logs": logs}

    def _poll_fetch_result(self):
        if self._fetch_result is None:
            QTimer.singleShot(150, self._poll_fetch_result)
            return
        result = self._fetch_result
        self._fetch_result = None
        self._fetch_in_progress = False
        self._set_busy("fetch", False)
        for line in result.get("logs", []):
            self._log(line)
        items = result.get("items", [])
        self.ctx.batch_store.set_all(items)
        self._current_page = 1
        self._status_label.setText(f"已加载 {len(items)} 件商品")
        self._refresh_view()

    def _match_prices(self):
        if self._match_in_progress:
            return
        items = self.ctx.batch_store.get_all()
        if not items:
            QMessageBox.information(self, "提示", "请先拉取商品列表")
            return

        self._match_in_progress = True
        self._match_result = None
        self._set_busy("match", True)
        self._status_label.setText("正在匹配建议价…")
        self._log(f"开始匹配建议价，共 {len(items)} 件商品")
        threading.Thread(target=self._do_match, args=(items,), daemon=True).start()
        QTimer.singleShot(150, self._poll_match_result)

    def _do_match(self, items):
        matched_items = []
        logs = []
        total = len(items)
        for index, item in enumerate(items, start=1):
            records = self.ctx.sold_cache.filter_by_model_key(item.model, item.condition, item.capacity, item.color)
            pricing = self.ctx.pricing_engine.calculate(records, item.model, item.condition, item.capacity, item.color)
            item.pricing = pricing
            item.floor_price = pricing.floor_price
            item.settle_price = pricing.settle_price
            item.confidence = str(pricing.confidence or "")
            item.rule_hit = pricing.rule_hit or ""
            new_price = self.ctx.rule_engine.apply(item, pricing)
            item.suggest_price = new_price or pricing.suggest_price()
            item.suggested_settle_price = PricingEngine.calc_settle_price(item.suggest_price) if item.suggest_price else None
            item.op_message = pricing.warning or pricing.rule_hit or "已匹配"
            item.op_status = "已匹配"
            matched_items.append(item)
            if index % 10 == 0 or index == total:
                logs.append(f"匹配进度 {index}/{total}")
        self._match_result = {"items": matched_items, "logs": logs}

    def _poll_match_result(self):
        if self._match_result is None:
            QTimer.singleShot(150, self._poll_match_result)
            return
        result = self._match_result
        self._match_result = None
        self._match_in_progress = False
        self._set_busy("match", False)
        for line in result.get("logs", []):
            self._log(line)
        self.ctx.batch_store.set_all(result.get("items", []))
        self._status_label.setText("建议价匹配完成")
        self._refresh_view()

    def _batch_reprice(self):
        if self._reprice_in_progress:
            return
        items = self.ctx.batch_store.get_all()
        targets = [item for item in items if item.suggest_price and abs(item.suggest_price - item.current_price) >= 1]
        if not targets:
            QMessageBox.information(self, "提示", "没有需要改价的商品（差价 < 1 元自动跳过）")
            return
        confirmed = QMessageBox.question(self, "确认", f"即将对 {len(targets)} 件商品执行改价，确认继续？")
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        self._reprice_in_progress = True
        self._reprice_result = None
        self._set_busy("reprice", True)
        self._status_label.setText(f"正在批量改价，共 {len(targets)} 件…")
        self._log(f"开始批量改价，共 {len(targets)} 件")
        threading.Thread(target=self._do_reprice, args=(targets,), daemon=True).start()
        QTimer.singleShot(150, self._poll_reprice_result)

    def _do_reprice(self, targets):
        ok = 0
        fail = 0
        logs = []
        history_records = []
        by_account = defaultdict(list)
        for item in targets:
            by_account[item.account_name].append(item)

        account_services = {}
        for account in self.ctx.account_store.enabled_accounts():
            account_services[account.name] = ImeiService(account.name, account.cookie)

        for account_name, account_items in by_account.items():
            service = account_services.get(account_name)
            if service is None:
                for item in account_items:
                    item.reprice_ok = False
                    item.reprice_msg = "账号不存在"
                    item.op_status = "失败"
                    item.op_message = "账号不存在"
                    fail += 1
                continue
            for item in account_items:
                new_price = round(item.suggest_price, 0)
                try:
                    success, message = service.change_price(item.product_id, new_price)
                except Exception as exc:
                    success, message = False, str(exc)
                item.new_price = new_price
                item.reprice_ok = success
                item.reprice_msg = message
                item.op_status = "成功" if success else "失败"
                item.op_message = message
                if success:
                    ok += 1
                    settle = PricingEngine.calc_settle_price(new_price)
                    history_records.append(
                        PriceChangeRecord(
                            id=None,
                            timestamp=datetime.datetime.now(),
                            product_id=item.product_id,
                            qc_code=item.qc_code,
                            title=item.title,
                            model=item.model,
                            condition=item.condition,
                            capacity=item.capacity,
                            color=item.color,
                            old_price=item.current_price,
                            new_price=new_price,
                            diff=new_price - item.current_price,
                            settle_price=settle,
                            trigger=PriceTrigger.MANUAL,
                            account_name=account_name,
                        )
                    )
                    item.current_price = new_price
                else:
                    fail += 1
                logs.append(f"[{account_name}] {item.qc_code or item.product_id}: {'成功' if success else '失败'} - {message}")

        if history_records:
            self.ctx.history_db.record_many(history_records)
        self._reprice_result = {
            "items": self.ctx.batch_store.get_all(),
            "logs": logs,
            "ok": ok,
            "fail": fail,
        }

    def _poll_reprice_result(self):
        if self._reprice_result is None:
            QTimer.singleShot(150, self._poll_reprice_result)
            return
        result = self._reprice_result
        self._reprice_result = None
        self._reprice_in_progress = False
        self._set_busy("reprice", False)
        for line in result.get("logs", []):
            self._log(line)
        self.ctx.batch_store.set_all(result.get("items", []))
        self._status_label.setText(f"批量改价完成：成功 {result.get('ok', 0)}，失败 {result.get('fail', 0)}")
        self._refresh_view()

    def _filtered_items(self):
        items = self.ctx.batch_store.get_all()
        keyword = self._filter_input.text().strip().lower()
        if not keyword:
            return items
        return [
            item for item in items
            if keyword in (item.title or "").lower()
            or keyword in (item.qc_code or "").lower()
            or keyword in (item.model or "").lower()
        ]

    def _refresh_view(self):
        items = self._filtered_items()
        total = len(items)
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._current_page = min(max(1, self._current_page), total_pages)
        start = (self._current_page - 1) * self.PAGE_SIZE
        page_items = items[start:start + self.PAGE_SIZE]

        self._table.setRowCount(len(page_items))
        for row, item in enumerate(page_items):
            diff = item.price_diff
            settle = item.suggested_settle_price or (PricingEngine.calc_settle_price(item.suggest_price) if item.suggest_price else None)
            confidence = self._format_confidence(item.pricing.confidence if item.pricing else item.confidence)
            sample_count = item.pricing.sample_count if item.pricing else 0
            status_text = item.reprice_msg or item.op_message or item.op_status or "待处理"
            values = [
                item.qc_code,
                (item.title or "")[:28],
                item.condition,
                item.capacity,
                self._format_price(item.current_price),
                self._format_price(item.suggest_price),
                f"{diff:+.0f}" if diff is not None else "—",
                self._format_price(settle),
                str(sample_count),
                confidence,
                status_text,
                item.account_name,
            ]
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(value))

        self._page_label.setText(f"第 {self._current_page}/{total_pages} 页")
        self._total_label.setText(f"共 {total} 条")
        self._refresh_summary(items, total_pages)

    def _refresh_summary(self, items, total_pages: int):
        matched = sum(1 for item in items if item.pricing is not None)
        pending = sum(1 for item in items if item.suggest_price and abs(item.suggest_price - item.current_price) >= 1)
        success = sum(1 for item in items if item.reprice_ok is True)
        failed = sum(1 for item in items if item.reprice_ok is False)
        self._summary_labels["total"].setText(str(len(items)))
        self._summary_labels["matched"].setText(str(matched))
        self._summary_labels["pending"].setText(str(pending))
        self._summary_labels["success"].setText(str(success))
        self._summary_labels["failed"].setText(str(failed))
        self._summary_labels["page"].setText(f"{self._current_page}/{total_pages}")

    def _on_filter_changed(self):
        self._current_page = 1
        self._refresh_view()

    def _prev_page(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._refresh_view()

    def _next_page(self):
        total = len(self._filtered_items())
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self._current_page < total_pages:
            self._current_page += 1
            self._refresh_view()

    def _clear_list(self):
        items = self.ctx.batch_store.get_all()
        if not items:
            return
        confirmed = QMessageBox.question(self, "清空列表", f"确认清空当前 {len(items)} 条批量商品记录？")
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self.ctx.batch_store.set_all([])
        self._current_page = 1
        self._status_label.setText("已清空批量商品列表")
        self._log("已清空批量商品列表")
        self._refresh_view()

    def _set_busy(self, action: str, busy: bool):
        if action == "fetch":
            self._fetch_btn.setEnabled(not busy)
        elif action == "match":
            self._match_btn.setEnabled(not busy)
        elif action == "reprice":
            self._reprice_btn.setEnabled(not busy)

    def _format_price(self, value):
        if value is None:
            return "—"
        return f"¥{value:,.0f}"

    def _format_confidence(self, value):
        mapping = {
            ConfidenceLevel.HIGH: "高 ✓",
            ConfidenceLevel.LOW: "低 ⚠",
            ConfidenceLevel.NONE: "无数据",
            "high": "高 ✓",
            "low": "低 ⚠",
            "none": "无数据",
        }
        return mapping.get(value, str(value or "—"))

    def _log(self, message: str):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_text.appendPlainText(f"[{timestamp}] {message}")
