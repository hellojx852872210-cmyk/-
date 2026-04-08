# -*- coding: utf-8 -*-
from __future__ import annotations

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
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


class ZhuanzhuanImeiQtTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._current_detail = None
        self._current_pricing = None
        self._query_in_progress = False
        self._query_result = None
        self._action_in_progress = False
        self._action_result = None
        self._preview_result = None
        self._preview_request_token = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._request_preview_settle)
        self._build_ui()
        self._refresh_accounts()
        self._update_preview_settle()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel("该页面用于按 IMEI / 质检码查询单品、查看缓存定价建议、预估到手价，并执行改价 / 上架。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_query_box())
        layout.addWidget(self._build_detail_box())
        layout.addWidget(self._build_pricing_box())
        layout.addWidget(self._build_action_box())
        layout.addWidget(self._build_sample_box(), 1)

    def _build_query_box(self):
        box = QGroupBox("查询商品")
        layout = QGridLayout(box)

        layout.addWidget(QLabel("账号"), 0, 0)
        self._account_combo = QComboBox()
        layout.addWidget(self._account_combo, 0, 1)

        layout.addWidget(QLabel("质检码 / IMEI"), 0, 2)
        self._code_input = QLineEdit()
        self._code_input.setPlaceholderText("输入 15 位 IMEI 或质检码")
        self._code_input.returnPressed.connect(self._query)
        layout.addWidget(self._code_input, 0, 3)

        self._query_btn = QPushButton("查询商品")
        self._query_btn.clicked.connect(self._query)
        layout.addWidget(self._query_btn, 0, 4)

        self._query_status = QLabel("请选择账号并输入质检码 / IMEI。")
        self._query_status.setWordWrap(True)
        layout.addWidget(self._query_status, 1, 0, 1, 5)

        layout.setColumnStretch(3, 1)
        return box

    def _build_detail_box(self):
        box = QGroupBox("商品信息")
        layout = QGridLayout(box)

        fields = [
            ("title", "商品名"),
            ("model", "型号"),
            ("condition", "成色"),
            ("capacity", "容量"),
            ("color", "颜色"),
            ("status", "状态"),
            ("current_price", "当前挂牌价"),
            ("settle_price", "当前到手价"),
            ("cost_price", "成本价"),
        ]
        self._detail_labels = {}
        for index, (key, label) in enumerate(fields):
            row = index // 3
            col = (index % 3) * 2
            layout.addWidget(QLabel(label), row, col)
            value_label = QLabel("—")
            value_label.setWordWrap(True)
            self._detail_labels[key] = value_label
            layout.addWidget(value_label, row, col + 1)

        return box

    def _build_pricing_box(self):
        box = QGroupBox("定价建议")
        layout = QVBoxLayout(box)

        self._summary_labels = {}
        grid = QGridLayout()
        specs = [
            ("fast_price", "极速动销价"),
            ("cons_price", "保守动销价"),
            ("floor_price", "底价预警"),
            ("preview_settle", "预计到手价"),
            ("sample", "样本数量"),
            ("confidence", "置信度"),
        ]
        for index, (key, label) in enumerate(specs):
            row = index // 3
            col = (index % 3) * 2
            grid.addWidget(QLabel(label), row, col)
            value_label = QLabel("—")
            value_label.setWordWrap(True)
            self._summary_labels[key] = value_label
            grid.addWidget(value_label, row, col + 1)
        layout.addLayout(grid)

        self._warning_label = QLabel("查询后显示样本质量和规则说明。")
        self._warning_label.setWordWrap(True)
        layout.addWidget(self._warning_label)

        self._explain_text = QPlainTextEdit()
        self._explain_text.setReadOnly(True)
        self._explain_text.setPlaceholderText("查询后显示规则解释")
        self._explain_text.setMaximumHeight(110)
        layout.addWidget(self._explain_text)

        return box

    def _build_action_box(self):
        box = QGroupBox("改价 / 上架")
        layout = QVBoxLayout(box)

        form = QFormLayout()
        self._new_price_input = QLineEdit()
        self._new_price_input.setPlaceholderText("输入目标价格")
        self._new_price_input.textChanged.connect(self._update_preview_settle)
        self._new_price_input.returnPressed.connect(self._change_price)
        form.addRow("目标价格", self._new_price_input)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self._use_fast_btn = QPushButton("使用极速价")
        self._use_cons_btn = QPushButton("使用保守价")
        self._change_btn = QPushButton("执行改价")
        self._list_btn = QPushButton("执行上架")
        btn_row.addWidget(self._use_fast_btn)
        btn_row.addWidget(self._use_cons_btn)
        btn_row.addWidget(self._change_btn)
        btn_row.addWidget(self._list_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._action_status = QLabel("查询后可直接带入建议价执行。")
        self._action_status.setWordWrap(True)
        layout.addWidget(self._action_status)

        self._action_log = QPlainTextEdit()
        self._action_log.setReadOnly(True)
        self._action_log.setMaximumHeight(100)
        layout.addWidget(self._action_log)

        self._use_fast_btn.clicked.connect(lambda: self._fill_price("fast"))
        self._use_cons_btn.clicked.connect(lambda: self._fill_price("cons"))
        self._change_btn.clicked.connect(self._change_price)
        self._list_btn.clicked.connect(self._list_product)

        return box

    def _build_sample_box(self):
        box = QGroupBox("参考成交记录")
        layout = QVBoxLayout(box)

        self._sample_table = QTableWidget(0, 5)
        self._sample_table.setHorizontalHeaderLabels(["成交时间", "成交价", "动销时长(h)", "来源", "标题"])
        self._sample_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._sample_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._sample_table.setAlternatingRowColors(True)
        self._sample_table.verticalHeader().setVisible(False)
        self._sample_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._sample_table, 1)

        self._sample_hint = QLabel("快销样本会标注“快销”。")
        self._sample_hint.setWordWrap(True)
        layout.addWidget(self._sample_hint)

        return box

    def _refresh_accounts(self):
        current = self._account_combo.currentText().strip()
        names = [account.name for account in self.ctx.account_store.enabled_accounts()]
        self._account_combo.blockSignals(True)
        self._account_combo.clear()
        self._account_combo.addItems(names)
        if current and current in names:
            self._account_combo.setCurrentText(current)
        elif names:
            self._account_combo.setCurrentIndex(0)
        self._account_combo.blockSignals(False)

    def _query(self):
        if self._query_in_progress:
            return
        code = self._code_input.text().strip()
        account_name = self._account_combo.currentText().strip()
        if not code or not account_name:
            QMessageBox.warning(self, "提示", "请先填写质检码 / IMEI 并选择账号")
            return

        self._query_in_progress = True
        self._query_result = None
        self._query_btn.setEnabled(False)
        self._query_status.setText("查询中…")
        self._log_action(f"开始查询：{code}（账号：{account_name}）")
        threading.Thread(target=self._do_query, args=(code, account_name), daemon=True).start()
        QTimer.singleShot(150, self._poll_query_result)

    def _do_query(self, code: str, account_name: str):
        detail, error = self._fetch_detail(code, account_name)
        if error:
            self._query_result = {"error": error}
            return
        if detail is None:
            self._query_result = {"error": "未找到商品"}
            return

        cost = self.ctx.cost_map.get(detail.product_id)
        if cost:
            detail.cost_price = cost
        records = self.ctx.sold_cache.filter_by_model_key(detail.model, detail.condition, detail.capacity, detail.color)
        pricing = self.ctx.pricing_engine.calculate(records, detail.model, detail.condition, detail.capacity, detail.color)
        item = BatchItem(
            product_id=detail.product_id,
            qc_code=detail.qc_code,
            title=detail.title,
            current_price=detail.current_price,
            status=detail.status,
            account_name=account_name,
            imei=detail.imei,
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            cost_price=detail.cost_price,
            listed_time=detail.listed_time,
            pricing=pricing,
        )
        explain_lines = self.ctx.rule_engine.explain(item, pricing)
        self._query_result = {
            "detail": detail,
            "pricing": pricing,
            "records": list(pricing.raw_records),
            "explain_lines": explain_lines,
        }

    def _fetch_detail(self, code: str, account_name: str):
        accounts = {account.name: account for account in self.ctx.account_store.load_all()}
        account = accounts.get(account_name)
        if not account:
            return None, "账号不存在"
        try:
            service = ImeiService(account.name, account.cookie)
            if len(code) == 15 and code.isdigit():
                return service.query_by_imei(code), None
            return service.query_by_qc_code(code), None
        except Exception as exc:
            return None, str(exc)

    def _poll_query_result(self):
        if self._query_result is None:
            QTimer.singleShot(150, self._poll_query_result)
            return

        result = self._query_result
        self._query_result = None
        self._query_in_progress = False
        self._query_btn.setEnabled(True)

        if result.get("error"):
            self._query_status.setText("查询失败")
            self._action_status.setText("查询失败")
            QMessageBox.critical(self, "查询失败", result["error"])
            self._log_action(f"查询失败：{result['error']}")
            return

        self._apply_query_result(result)

    def _apply_query_result(self, result: dict):
        detail = result["detail"]
        pricing = result["pricing"]
        records = result["records"]
        explain_lines = result["explain_lines"]
        self._current_detail = detail
        self._current_pricing = pricing

        self._detail_labels["title"].setText(detail.title or "—")
        self._detail_labels["model"].setText(detail.model or "—")
        self._detail_labels["condition"].setText(detail.condition or "—")
        self._detail_labels["capacity"].setText(detail.capacity or "—")
        self._detail_labels["color"].setText(detail.color or "—")
        self._detail_labels["status"].setText(self._format_status(detail))
        self._detail_labels["current_price"].setText(self._format_price(detail.current_price))
        current_settle = detail.settle_price if detail.settle_price and detail.settle_price > 0 else None
        self._detail_labels["settle_price"].setText(self._format_price(current_settle, digits=1))
        self._detail_labels["cost_price"].setText(self._format_price(detail.cost_price or None))

        self._summary_labels["fast_price"].setText(self._format_price(pricing.fast_price))
        self._summary_labels["cons_price"].setText(self._format_price(pricing.cons_price))
        self._summary_labels["floor_price"].setText(self._format_price(pricing.floor_price))
        self._summary_labels["sample"].setText(str(pricing.sample_count or 0))
        self._summary_labels["confidence"].setText(self._format_confidence(pricing.confidence))
        self._warning_label.setText(pricing.warning or "样本充足，可直接参考建议价。")
        self._explain_text.setPlainText("\n".join(explain_lines))
        self._refresh_sample_table(records)
        self._update_preview_settle()

        if records:
            self._query_status.setText(f"查询完成，命中 {len(records)} 条样本")
        else:
            self._query_status.setText("查询完成，但当前条件下暂无成交样本")
        self._action_status.setText("可使用建议价或输入新价格后执行。")
        self._log_action(f"查询完成：{detail.title}")

    def _refresh_sample_table(self, records):
        self._sample_table.setRowCount(len(records))
        fast_hours = getattr(self.ctx.pricing_engine, "fast_hours", 24)
        for row, record in enumerate(sorted(records, key=lambda item: item.sold_time, reverse=True)):
            is_fast = record.hours_to_sell is not None and record.hours_to_sell <= fast_hours
            title = record.title[:40]
            if is_fast:
                title = f"[快销] {title}"
            values = [
                record.sold_time.strftime("%m-%d %H:%M"),
                self._format_price(record.sold_price),
                f"{record.hours_to_sell:.1f}" if record.hours_to_sell is not None else "—",
                record.source,
                title,
            ]
            for column, value in enumerate(values):
                self._sample_table.setItem(row, column, QTableWidgetItem(value))
        self._sample_hint.setText("暂无参考成交记录。" if not records else "快销样本会标注“快销”。")

    def _fill_price(self, which: str):
        if not self._current_pricing:
            return
        price = self._current_pricing.fast_price if which == "fast" else self._current_pricing.cons_price
        if price:
            self._new_price_input.setText(str(round(price)))

    def _parse_new_price(self):
        text = self._new_price_input.text().strip().replace(",", "")
        if not text:
            return None
        try:
            price = float(text)
        except ValueError:
            return None
        return price if price > 0 else None

    def _update_preview_settle(self):
        self._preview_timer.stop()
        token = self._preview_request_token + 1
        self._preview_request_token = token
        price = self._parse_new_price()
        if price is None or not self._current_detail or not self._account_combo.currentText().strip():
            self._summary_labels["preview_settle"].setText("—")
            return
        self._summary_labels["preview_settle"].setText("查询中…")
        self._preview_timer.start(300)

    def _request_preview_settle(self):
        price = self._parse_new_price()
        detail = self._current_detail
        account_name = self._account_combo.currentText().strip()
        token = self._preview_request_token
        if price is None or not detail or not account_name:
            self._summary_labels["preview_settle"].setText("—")
            return
        self._preview_result = None
        threading.Thread(
            target=self._do_preview_settle,
            args=(detail.product_id, account_name, price, token),
            daemon=True,
        ).start()
        QTimer.singleShot(120, self._poll_preview_result)

    def _do_preview_settle(self, product_id: str, account_name: str, price: float, token: int):
        settle = None
        try:
            accounts = {account.name: account for account in self.ctx.account_store.load_all()}
            account = accounts.get(account_name)
            if account is not None:
                service = ImeiService(account.name, account.cookie)
                settle = service.estimate_settle_price(product_id, price)
        except Exception:
            settle = None
        self._preview_result = {"token": token, "settle": settle}

    def _poll_preview_result(self):
        if self._preview_result is None:
            QTimer.singleShot(120, self._poll_preview_result)
            return
        result = self._preview_result
        self._preview_result = None
        if result["token"] != self._preview_request_token:
            return
        self._summary_labels["preview_settle"].setText(self._format_price(result.get("settle"), digits=1))

    def _change_price(self):
        self._start_action("change")

    def _list_product(self):
        self._start_action("list")

    def _start_action(self, action: str):
        if self._action_in_progress:
            return
        if not self._current_detail:
            QMessageBox.warning(self, "提示", "请先查询商品")
            return
        price = self._parse_new_price()
        if price is None:
            QMessageBox.warning(self, "提示", "请输入有效目标价格")
            return
        account_name = self._account_combo.currentText().strip()
        if not account_name:
            QMessageBox.warning(self, "提示", "请选择账号")
            return

        self._action_in_progress = True
        self._action_result = None
        self._change_btn.setEnabled(False)
        self._list_btn.setEnabled(False)
        action_text = "改价" if action == "change" else "上架"
        self._action_status.setText(f"正在执行{action_text}…")
        self._log_action(f"开始{action_text}：{self._current_detail.title} -> {price:.0f}")
        threading.Thread(target=self._do_action, args=(action, account_name, price), daemon=True).start()
        QTimer.singleShot(150, self._poll_action_result)

    def _do_action(self, action: str, account_name: str, price: float):
        detail = self._current_detail
        accounts = {account.name: account for account in self.ctx.account_store.load_all()}
        account = accounts.get(account_name)
        if detail is None or account is None:
            self._action_result = {"ok": False, "message": "账号不存在", "action": action}
            return

        try:
            service = ImeiService(account.name, account.cookie)
            if action == "change":
                ok, message = service.change_price(detail, price)
            else:
                ok, message = service.list_product(detail.product_id, price, detail.qc_code)
            refreshed_detail = detail
            if ok:
                settle = PricingEngine.calc_settle_price(price)
                self.ctx.history_db.record(
                    PriceChangeRecord(
                        id=None,
                        product_id=detail.product_id,
                        qc_code=detail.qc_code,
                        title=detail.title,
                        model=detail.model,
                        condition=detail.condition,
                        capacity=detail.capacity,
                        color=detail.color,
                        old_price=detail.current_price,
                        new_price=price,
                        diff=price - detail.current_price,
                        settle_price=settle,
                        trigger=PriceTrigger.MANUAL,
                        account_name=account_name,
                    )
                )
                refreshed_detail = service.query_by_qc_code(detail.qc_code) or service.query_by_imei(detail.imei) or detail
            self._action_result = {
                "ok": ok,
                "message": message,
                "detail": refreshed_detail,
                "action": action,
            }
        except Exception as exc:
            self._action_result = {"ok": False, "message": str(exc), "action": action}

    def _poll_action_result(self):
        if self._action_result is None:
            QTimer.singleShot(150, self._poll_action_result)
            return

        result = self._action_result
        self._action_result = None
        self._action_in_progress = False
        self._change_btn.setEnabled(True)
        self._list_btn.setEnabled(True)

        ok = result.get("ok", False)
        message = result.get("message", "")
        action = result.get("action")
        action_text = "改价" if action == "change" else "上架"
        self._action_status.setText(f"{'✓' if ok else '✗'} {action_text}：{message}")
        self._log_action(f"{action_text}{'成功' if ok else '失败'}：{message}")

        if ok and result.get("detail") is not None:
            self._query_result = None
            self._apply_query_result({
                "detail": result["detail"],
                "pricing": self._current_pricing,
                "records": list(getattr(self._current_pricing, "raw_records", [])) if self._current_pricing else [],
                "explain_lines": self._explain_text.toPlainText().splitlines() if self._explain_text.toPlainText() else [],
            })
        if ok:
            QMessageBox.information(self, f"{action_text}成功", message)
        else:
            QMessageBox.critical(self, f"{action_text}失败", message)

    def _format_status(self, detail) -> str:
        status = getattr(detail, "status", None)
        label = getattr(status, "label", "")
        if label and label != "未知":
            return label
        status_text = str(getattr(detail, "status_text", "") or "").strip()
        if status_text and status_text not in {"-1", "UNKNOWN", "unknown"}:
            return status_text
        raw = getattr(status, "value", status)
        raw_text = str(raw or "").strip()
        return f"未知状态({raw_text})" if raw_text else "未知"

    def _format_price(self, value, digits: int = 0):
        if value is None:
            return "—"
        return f"¥{value:,.{digits}f}"

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

    def _log_action(self, message: str):
        self._action_log.appendPlainText(message)
