# -*- coding: utf-8 -*-
from __future__ import annotations

import threading

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
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

from ..automation.tasks import _pricing_preview, build_reprice_decision
from ..core.models import BatchItem, ConfidenceLevel
from ..core.pricing_engine import PricingEngine


class ZhuanzhuanCalcQtTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._boxes: dict[str, QComboBox] = {}
        self._all_options = {"model": [], "condition": [], "capacity": [], "color": []}
        self._query_in_progress = False
        self._query_result = None
        self._suspend_filter_refresh = False
        self._filter_refresh_timer = QTimer(self)
        self._filter_refresh_timer.setSingleShot(True)
        self._filter_refresh_timer.timeout.connect(self._refresh_dependent_options)
        self._build_ui()
        self._refresh_cache_options()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel("该页面用于基于本地成交缓存进行手动定价测算：支持筛选联动、模糊型号匹配、规则解释、参考成交样本和到手价计算。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_query_box())
        layout.addWidget(self._build_result_box())
        layout.addWidget(self._build_explain_box())
        layout.addWidget(self._build_sample_box(), 1)

    def _build_query_box(self):
        box = QGroupBox("查询条件")
        layout = QGridLayout(box)

        specs = [
            ("model", "型号"),
            ("condition", "成色"),
            ("capacity", "容量"),
            ("color", "颜色"),
        ]
        for row, (key, label) in enumerate(specs):
            layout.addWidget(QLabel(label), row, 0)
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setMinimumWidth(280 if key == "model" else 220)
            if key == "model":
                combo.setMaxVisibleItems(12)
                completer = QCompleter([], combo)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                combo.setCompleter(completer)
            combo.lineEdit().textEdited.connect(lambda _text, field=key: self._on_filter_text_edited(field))
            combo.currentTextChanged.connect(lambda _text, field=key: self._on_filter_value_changed(field))
            combo.lineEdit().returnPressed.connect(self._query)
            self._boxes[key] = combo
            layout.addWidget(combo, row, 1)

        btn_row = QHBoxLayout()
        self._query_btn = QPushButton("查询建议价")
        self._query_btn.clicked.connect(self._query)
        btn_row.addWidget(self._query_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row, 0, 2, 4, 1)

        layout.addWidget(QLabel("当前挂牌价"), 4, 0)
        self._current_price_input = QLineEdit()
        self._current_price_input.setPlaceholderText("输入当前挂牌价")
        self._current_price_input.returnPressed.connect(self._calc_settle)
        layout.addWidget(self._current_price_input, 4, 1)

        self._settle_btn = QPushButton("计算到手价")
        self._settle_btn.clicked.connect(self._calc_settle)
        layout.addWidget(self._settle_btn, 4, 2)

        self._query_status = QLabel("请选择型号后查询。")
        self._query_status.setWordWrap(True)
        layout.addWidget(self._query_status, 5, 0, 1, 3)

        layout.setColumnStretch(1, 1)
        return box

    def _build_result_box(self):
        box = QGroupBox("定价建议摘要")
        layout = QVBoxLayout(box)

        self._summary_labels = {}
        grid = QGridLayout()
        specs = [
            ("fast_price", "极速动销价"),
            ("cons_price", "保守动销价"),
            ("floor_price", "底价预警"),
            ("system_price", "系统建议价"),
            ("final_price", "最终执行价"),
            ("settle", "到手价"),
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

        self._warning_label = QLabel("查询后显示样本质量和补充说明。")
        self._warning_label.setWordWrap(True)
        layout.addWidget(self._warning_label)

        return box

    def _build_explain_box(self):
        box = QGroupBox("规则匹配解释")
        layout = QVBoxLayout(box)

        self._explain_text = QPlainTextEdit()
        self._explain_text.setReadOnly(True)
        self._explain_text.setPlaceholderText("查询后显示规则解释")
        self._explain_text.setMaximumHeight(120)
        layout.addWidget(self._explain_text)

        return box

    def _build_sample_box(self):
        box = QGroupBox("参考成交记录")
        layout = QVBoxLayout(box)

        self._sample_table = QTableWidget(0, 5)
        self._sample_table.setHorizontalHeaderLabels(["成交时间", "成交价", "动销时长(h)", "来源", "标题"])
        self._sample_table.setAlternatingRowColors(True)
        self._sample_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._sample_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._sample_table.verticalHeader().setVisible(False)
        header = self._sample_table.horizontalHeader()
        header.setStretchLastSection(True)
        layout.addWidget(self._sample_table, 1)

        self._sample_hint = QLabel("快销样本会标注“快销”。")
        self._sample_hint.setWordWrap(True)
        layout.addWidget(self._sample_hint)

        return box

    def _set_combo_items(self, key: str, values: list[str], current_text: str = ""):
        combo = self._boxes[key]
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("")
        combo.addItems(values)
        combo.setCurrentText(current_text)
        combo.blockSignals(False)
        if key == "model":
            completer = combo.completer()
            if completer is not None:
                completer.model().setStringList(values)

    def _score_model_match(self, keyword: str, value: str) -> tuple[int, int, str]:
        keyword_lower = keyword.lower()
        value_lower = value.lower()
        if not keyword_lower:
            return (0, len(value_lower), value_lower)
        if value_lower.startswith(keyword_lower):
            return (0, len(value_lower), value_lower)
        if keyword_lower in value_lower:
            return (1, value_lower.index(keyword_lower), value_lower)
        compact_keyword = "".join(keyword_lower.split())
        compact_value = "".join(value_lower.split())
        if compact_keyword and compact_keyword in compact_value:
            return (2, compact_value.index(compact_keyword), value_lower)
        positions = []
        seek_from = 0
        for char in keyword_lower:
            found = value_lower.find(char, seek_from)
            if found < 0:
                return (99, 99, value_lower)
            positions.append(found)
            seek_from = found + 1
        spread = positions[-1] - positions[0] if positions else len(value_lower)
        return (3, spread, value_lower)

    def _filter_model_values(self, keyword: str) -> list[str]:
        all_models = self._all_options.get("model", [])
        if not keyword:
            return all_models
        ranked = []
        for value in all_models:
            score = self._score_model_match(keyword, value)
            if score[0] < 99:
                ranked.append((score, value))
        ranked.sort(key=lambda item: (item[0][0], item[0][1], item[0][2]))
        return [value for _score, value in ranked[:80]]

    def _show_model_popup_if_needed(self):
        combo = self._boxes["model"]
        if combo.count() > 1 and combo.hasFocus():
            combo.showPopup()
        else:
            combo.hidePopup()

    def _pick_values(self, *groups: list[str]) -> list[str]:
        for values in groups:
            picked = []
            seen = set()
            for value in values:
                if value and value not in seen:
                    picked.append(value)
                    seen.add(value)
            if picked:
                return picked
        return []

    def _current_values(self) -> dict[str, str]:
        return {key: combo.currentText().strip() for key, combo in self._boxes.items()}

    def _should_use_fuzzy_model(self, model: str | None = None) -> bool:
        current = (model if model is not None else self._boxes["model"].currentText()).strip()
        if not current:
            return False
        return current not in set(self._all_options.get("model", []))

    def _keep_value_if_present(self, key: str, values: list[str]):
        current = self._boxes[key].currentText().strip()
        if current and current not in values:
            self._boxes[key].setCurrentText("")

    def _model_values(self) -> list[str]:
        combo = self._boxes["model"]
        return [combo.itemText(index) for index in range(combo.count()) if combo.itemText(index)]

    def _refresh_cache_options(self):
        self._all_options = self.ctx.sold_cache.get_filter_options()
        current_model = self._boxes["model"].currentText().strip() if "model" in self._boxes else ""
        self._set_combo_items("model", self._all_options["model"], current_model)
        self._refresh_dependent_options()

    def _refresh_dependent_options(self):
        if self._suspend_filter_refresh:
            return
        values = self._current_values()
        model = values["model"]
        fuzzy_model = self._should_use_fuzzy_model(model)

        self._suspend_filter_refresh = True
        try:
            filtered = self.ctx.sold_cache.get_filter_options(model=model, fuzzy_model=fuzzy_model)
            condition_values = self._pick_values(
                filtered["condition"],
                self._all_options.get("condition", []),
            )
            self._set_combo_items("condition", condition_values, values["condition"])
            self._keep_value_if_present("condition", condition_values)
            condition = self._boxes["condition"].currentText().strip()

            filtered = self.ctx.sold_cache.get_filter_options(
                model=model,
                condition=condition,
                fuzzy_model=fuzzy_model,
            )
            broader_capacity = self.ctx.sold_cache.get_filter_options(
                model=model,
                fuzzy_model=fuzzy_model,
            )
            capacity_values = self._pick_values(
                filtered["capacity"],
                broader_capacity["capacity"],
                self._all_options.get("capacity", []),
            )
            self._set_combo_items("capacity", capacity_values, values["capacity"])
            self._keep_value_if_present("capacity", capacity_values)
            capacity = self._boxes["capacity"].currentText().strip()

            filtered = self.ctx.sold_cache.get_filter_options(
                model=model,
                condition=condition,
                capacity=capacity,
                fuzzy_model=fuzzy_model,
            )
            broader_color = self.ctx.sold_cache.get_filter_options(
                model=model,
                capacity=capacity,
                fuzzy_model=fuzzy_model,
            )
            model_color = self.ctx.sold_cache.get_filter_options(
                model=model,
                fuzzy_model=fuzzy_model,
            )
            color_values = self._pick_values(
                filtered["color"],
                broader_color["color"],
                model_color["color"],
                self._all_options.get("color", []),
            )
            self._set_combo_items("color", color_values, values["color"])
            self._keep_value_if_present("color", color_values)
        finally:
            self._suspend_filter_refresh = False

    def _schedule_filter_refresh(self, delay_ms: int = 0):
        if delay_ms <= 0:
            self._filter_refresh_timer.stop()
            self._refresh_dependent_options()
            return
        self._filter_refresh_timer.start(delay_ms)

    def _on_filter_text_edited(self, key: str):
        if key == "model":
            current_text = self._boxes["model"].currentText()
            filtered = self._filter_model_values(current_text.strip())
            self._set_combo_items("model", filtered, current_text)
            self._show_model_popup_if_needed()
            self._schedule_filter_refresh(120)
            return
        self._schedule_filter_refresh()

    def _on_filter_value_changed(self, key: str):
        if self._suspend_filter_refresh:
            return
        if key == "model":
            self._show_model_popup_if_needed()
            self._schedule_filter_refresh(120)
            return
        self._schedule_filter_refresh()

    def _query(self):
        if self._query_in_progress:
            return
        values = self._current_values()
        model = values["model"]
        if not model:
            QMessageBox.information(self, "提示", "请先输入型号")
            return

        fuzzy_model = self._should_use_fuzzy_model(model)
        self._query_in_progress = True
        self._query_result = None
        self._query_btn.setEnabled(False)
        self._query_status.setText("查询中…")

        threading.Thread(
            target=self._do_query,
            args=(model, values["condition"], values["capacity"], values["color"], fuzzy_model),
            daemon=True,
        ).start()
        QTimer.singleShot(150, self._poll_query_result)

    def _do_query(self, model: str, condition: str, capacity: str, color: str, fuzzy_model: bool):
        try:
            records = self.ctx.sold_cache.find_records(
                model=model,
                condition=condition,
                capacity=capacity,
                color=color,
                fuzzy_model=fuzzy_model,
            )
            pricing = self.ctx.pricing_engine.calculate(records, model, condition, capacity, color)
            query_item = BatchItem(
                product_id="",
                qc_code="",
                title=model,
                model=model,
                condition=condition,
                capacity=capacity,
                color=color,
                current_price=0,
                pricing=pricing,
            )
            decision = build_reprice_decision(query_item, pricing, self.ctx.rule_engine, apply_rules=True)
            self._query_result = {
                "records": list(pricing.raw_records),
                "pricing": pricing,
                "decision": decision,
                "explain_lines": decision.get("explain_lines") or [],
                "preview": _pricing_preview(pricing, self.ctx.rule_engine, query_item, decision),
                "fuzzy_model": fuzzy_model,
            }
        except Exception as exc:
            self._query_result = {"error": str(exc)}

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
            QMessageBox.critical(self, "查询失败", result["error"])
            return

        self._apply_query_result(result)

    def _apply_query_result(self, result: dict):
        pricing = result["pricing"]
        decision = result.get("decision") or {}
        effective_pricing = decision.get("pricing") or pricing
        records = result["records"]
        explain_lines = result["explain_lines"]
        preview = result.get("preview", "")
        fuzzy_model = result.get("fuzzy_model", False)

        self._summary_labels["fast_price"].setText(self._format_price(pricing.fast_price))
        self._summary_labels["cons_price"].setText(self._format_price(pricing.cons_price))
        self._summary_labels["floor_price"].setText(self._format_price(pricing.floor_price))
        self._summary_labels["system_price"].setText(self._format_price(decision.get("system_price")))
        self._summary_labels["final_price"].setText(self._format_price(decision.get("final_price")))
        self._summary_labels["sample"].setText(str(pricing.sample_count or 0))
        self._summary_labels["confidence"].setText(self._format_confidence(pricing.confidence))
        current_price = self._parse_current_price()
        settle_value = PricingEngine.calc_settle_price(current_price) if current_price is not None else decision.get("final_settle_price")
        self._summary_labels["settle"].setText(self._format_price(settle_value))

        warning_parts = []
        if fuzzy_model:
            warning_parts.append("当前使用模糊型号匹配")
        if effective_pricing.warning:
            warning_parts.append(effective_pricing.warning)
        if preview:
            warning_parts.append(preview)
        self._warning_label.setText("；".join(warning_parts) if warning_parts else "样本充足，可直接参考建议价。")

        self._explain_text.setPlainText("\n".join(explain_lines))
        self._refresh_sample_table(records)

        if records:
            self._query_status.setText(f"查询完成，命中 {len(records)} 条样本")
        else:
            self._query_status.setText("当前条件下暂无成交样本")

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
        if not records:
            self._sample_hint.setText("暂无参考成交记录。")
        else:
            self._sample_hint.setText("快销样本会标注“快销”。")

    def _parse_current_price(self):
        text = self._current_price_input.text().strip().replace(",", "")
        if not text:
            return None
        try:
            price = float(text)
        except ValueError:
            return None
        if price <= 0:
            return None
        return price

    def _calc_settle(self):
        price = self._parse_current_price()
        if price is None:
            QMessageBox.information(self, "提示", "请输入有效挂牌价")
            return
        settle = PricingEngine.calc_settle_price(price)
        self._summary_labels["settle"].setText(self._format_price(settle))

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
