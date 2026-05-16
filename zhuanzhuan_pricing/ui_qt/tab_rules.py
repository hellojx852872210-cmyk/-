# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.models import PricingRule


class RulesQtTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._selected_index = -1
        self._build_ui()
        self._refresh_list()
        self._new_rule()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "统一承载跨平台定价规则。当前 Qt 页直接复用现有规则引擎语义，支持列表浏览、JSON 编辑、新建、保存和删除。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        splitter = QSplitter()
        splitter.addWidget(self._build_list_box())
        splitter.addWidget(self._build_editor_box())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

    def _build_list_box(self):
        box = QGroupBox("规则列表")
        layout = QVBoxLayout(box)

        self._rule_list = QListWidget()
        self._rule_list.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self._rule_list, 1)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("新增")
        delete_btn = QPushButton("删除")
        refresh_btn = QPushButton("刷新")
        btn_row.addWidget(new_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._list_hint = QLabel("按优先级顺序显示当前规则。")
        self._list_hint.setWordWrap(True)
        layout.addWidget(self._list_hint)

        new_btn.clicked.connect(self._new_rule)
        delete_btn.clicked.connect(self._delete_rule)
        refresh_btn.clicked.connect(self._refresh_list)

        return box

    def _build_editor_box(self):
        box = QGroupBox("规则详情")
        layout = QVBoxLayout(box)

        form = QFormLayout()
        self._name_input = QLineEdit()
        self._priority_input = QSpinBox()
        self._priority_input.setRange(1, 999)
        self._priority_input.setValue(50)
        self._enabled_input = QCheckBox("启用此规则")
        self._enabled_input.setChecked(True)
        self._note_input = QLineEdit()

        form.addRow("规则名称", self._name_input)
        form.addRow("优先级（越小越先）", self._priority_input)
        form.addRow("", self._enabled_input)
        form.addRow("备注", self._note_input)
        layout.addLayout(form)

        layout.addWidget(QLabel("匹配条件（JSON）"))
        self._match_text = QPlainTextEdit()
        self._match_text.setPlaceholderText('{\n  "model_contains": ""\n}')
        self._match_text.setMinimumHeight(150)
        layout.addWidget(self._match_text)

        layout.addWidget(QLabel("执行动作（JSON）"))
        self._action_text = QPlainTextEdit()
        self._action_text.setPlaceholderText('{\n  "type": "fast_price",\n  "adjust_pct": 0\n}')
        self._action_text.setMinimumHeight(150)
        layout.addWidget(self._action_text)

        help_text = QLabel(
            "匹配条件可用字段：model_contains、condition_in、capacity_in、stale_days_gte、stale_days_lt、cost_gte、cost_lt。\n"
            "执行动作类型：fast_price / conservative_price / fixed_drop / pct_drop / floor_price。"
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存规则")
        clear_btn = QPushButton("新建空白")
        btn_row.addStretch(1)
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        self._status_label = QLabel("请选择规则或新建规则。")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        save_btn.clicked.connect(self._save_rule)
        clear_btn.clicked.connect(self._new_rule)

        return box

    def _refresh_list(self):
        selected_row = self._selected_index
        self._rule_list.clear()
        for rule in self.ctx.rule_engine.rules:
            prefix = "✅" if rule.enabled else "⬜"
            self._rule_list.addItem(f"{prefix} [{rule.priority}] {rule.name}")
        if 0 <= selected_row < self._rule_list.count():
            self._rule_list.setCurrentRow(selected_row)
        self._list_hint.setText(f"当前共 {self._rule_list.count()} 条规则。")

    def _on_select(self):
        row = self._rule_list.currentRow()
        if row < 0:
            return
        self._selected_index = row
        rule = self.ctx.rule_engine.rules[row]
        payload = rule.to_dict()

        self._name_input.setText(rule.name)
        self._priority_input.setValue(rule.priority)
        self._enabled_input.setChecked(rule.enabled)
        self._note_input.setText(rule.note)
        self._match_text.setPlainText(json.dumps(payload.get("match", {}), ensure_ascii=False, indent=2))
        self._action_text.setPlainText(json.dumps(payload.get("action", {}), ensure_ascii=False, indent=2))
        self._status_label.setText(f"已加载规则：{rule.name}")

    def _save_rule(self):
        try:
            match = json.loads(self._match_text.toPlainText().strip() or "{}")
            action = json.loads(self._action_text.toPlainText().strip() or "{}")
        except json.JSONDecodeError as exc:
            QMessageBox.critical(self, "JSON 格式错误", str(exc))
            return

        try:
            rule = PricingRule.from_dict(
                {
                    "name": self._name_input.text().strip(),
                    "priority": self._priority_input.value(),
                    "enabled": self._enabled_input.isChecked(),
                    "match": match,
                    "action": action,
                    "note": self._note_input.text().strip(),
                }
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        if not rule.name:
            QMessageBox.information(self, "提示", "规则名称不能为空")
            return

        if self._selected_index >= 0:
            self.ctx.rule_engine.update_rule(self._selected_index, rule)
            saved_index = self._selected_index
            action_text = "更新"
        else:
            self.ctx.rule_engine.add_rule(rule)
            saved_index = len(self.ctx.rule_engine.rules) - 1
            action_text = "新增"

        self._selected_index = saved_index
        self._refresh_list()
        self._status_label.setText(f"规则已{action_text}：{rule.name}")
        QMessageBox.information(self, "保存", f"规则「{rule.name}」已保存")

    def _new_rule(self):
        self._selected_index = -1
        self._rule_list.clearSelection()
        self._name_input.clear()
        self._priority_input.setValue(50)
        self._enabled_input.setChecked(True)
        self._note_input.clear()
        self._match_text.setPlainText('{\n  "model_contains": ""\n}')
        self._action_text.setPlainText('{\n  "type": "fast_price",\n  "adjust_pct": 0\n}')
        self._status_label.setText("新规则模式：填写后点击“保存规则”。")

    def _delete_rule(self):
        row = self._rule_list.currentRow()
        if row < 0:
            return
        rule = self.ctx.rule_engine.rules[row]
        confirmed = QMessageBox.question(self, "删除", f"确认删除规则「{rule.name}」？")
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self.ctx.rule_engine.delete_rule(row)
        self._selected_index = -1
        self._refresh_list()
        self._new_rule()
        self._status_label.setText(f"规则已删除：{rule.name}")
