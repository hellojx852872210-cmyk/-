# -*- coding: utf-8 -*-
"""全局设置浮窗 — 费率/引擎参数/阈值"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import AppContext


class SettingsWindow(tk.Toplevel):
    def __init__(self, parent, ctx: "AppContext"):
        super().__init__(parent)
        self.ctx = ctx
        self.title("🔧 全局设置")
        self.geometry("520x540")
        self.resizable(False, False)
        self._fields = {}
        self._custom_offset_mode_var = tk.StringVar(value=self.ctx.cfg_val("auto_reprice_custom_offset_mode", "off"))
        self._custom_offset_value_var = tk.DoubleVar(value=self.ctx.cfg_val("auto_reprice_custom_offset_value", 0.0))
        self._build_ui()

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        fee_frame = ttk.Frame(nb)
        nb.add(fee_frame, text="平台费率")
        self._add_field(fee_frame, 0, "平台扣点率 (%)", "platform_fee_rate",
                        default=self.ctx.cfg_val("platform_fee_rate", 5.0),
                        scale=100)
        self._add_field(fee_frame, 1, "站点服务费 (元)", "station_service_fee",
                        default=self.ctx.cfg_val("station_service_fee", 40))
        ttk.Label(fee_frame, text="※ 不同店铺费率不同时，可在此调整",
                  foreground="gray").grid(row=2, column=0, columnspan=2,
                                          padx=8, pady=4, sticky="w")

        engine_frame = ttk.Frame(nb)
        nb.add(engine_frame, text="定价引擎")
        engine_fields = [
            ("参考窗口 (天)",       "engine_window",    60),
            ("近期分界 (天)",       "engine_near",      30),
            ("近期权重",            "engine_w_recent",   3),
            ("远期权重",            "engine_w_old",      1),
            ("最小样本量",          "engine_min_sample", 5),
            ("极速动销阈值 (小时)", "fast_sale_hours",  24),
        ]
        for row, (label, key, default) in enumerate(engine_fields):
            self._add_field(engine_frame, row, label, key,
                            default=self.ctx.cfg_val(key, default))

        auto_frame = ttk.Frame(nb)
        nb.add(auto_frame, text="自动化")
        auto_fields = [
            ("滞销第一阶 (天)",   "stale_stage1_days",     7),
            ("滞销第二阶 (天)",   "stale_stage2_days",    14),
            ("第二阶降价幅度 (%)", "stale_stage2_drop_pct", 5),
            ("自动调价间隔 (分钟)", "auto_reprice_interval", 120),
            ("自动上架间隔 (分钟)", "auto_list_interval", 120),
            ("播报间隔 (分钟)",   "sales_report_interval", 60),
        ]
        for row, (label, key, default) in enumerate(auto_fields):
            self._add_field(auto_frame, row, label, key,
                            default=self.ctx.cfg_val(key, default))

        offset_row = len(auto_fields)
        ttk.Separator(auto_frame, orient="horizontal").grid(row=offset_row, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 8))
        ttk.Label(auto_frame, text="自动调价自定义偏移:").grid(row=offset_row + 1, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(
            auto_frame,
            textvariable=self._custom_offset_mode_var,
            state="readonly",
            width=14,
            values=("off", "fixed", "percent"),
        ).grid(row=offset_row + 1, column=1, sticky="w", padx=8)
        ttk.Label(auto_frame, text="偏移值:").grid(row=offset_row + 2, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(auto_frame, textvariable=self._custom_offset_value_var, width=12).grid(
            row=offset_row + 2, column=1, sticky="w", padx=8
        )
        ttk.Label(
            auto_frame,
            text="off=关闭；fixed=固定加减金额；percent=按系统建议价百分比加减。",
            foreground="gray",
        ).grid(row=offset_row + 3, column=0, columnspan=2, padx=8, pady=(0, 4), sticky="w")

        ttk.Button(self, text="💾 保存所有设置",
                   command=self._save).pack(pady=8)

    def _add_field(self, parent, row: int, label: str, key: str,
                   default=0, scale=1):
        ttk.Label(parent, text=f"{label}:").grid(
            row=row, column=0, sticky="w", padx=8, pady=4)
        var = tk.DoubleVar(value=default * scale if scale != 1 else default)
        self._fields[key] = (var, scale)
        ttk.Entry(parent, textvariable=var, width=12).grid(
            row=row, column=1, sticky="w", padx=8)

    def _save(self):
        from ..config import cfg
        for key, (var, scale) in self._fields.items():
            val = var.get() / scale if scale != 1 else var.get()
            cfg.set(key, val)
        cfg.set("auto_reprice_custom_offset_mode", self._custom_offset_mode_var.get().strip() or "off")
        cfg.set("auto_reprice_custom_offset_value", self._custom_offset_value_var.get())
        messagebox.showinfo("保存", "设置已保存，部分参数重启后生效")


# 注入辅助方法到 AppContext
def _cfg_val(self, key, default=None):
    from .config import cfg
    return cfg.get(key, default)
