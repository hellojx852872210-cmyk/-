# -*- coding: utf-8 -*-
"""
自动化 Tab — 调度器控制 + ERP 导入商品 + 企业微信配置
"""
from __future__ import annotations
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from ..services.reprice_service import run_reprice_pipeline
from ..config import cfg

if TYPE_CHECKING:
    from .app import AppContext


class AutoTab:
    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        self._event_target = parent
        self._erp_sync_running = False
        self._manage_window = None
        self._last_import_summary = ""
        self._custom_offset_mode_var = tk.StringVar(value=self._normalize_offset_mode(self.ctx.cfg_val("auto_reprice_custom_offset_mode", "off")))
        self._custom_offset_value_var = tk.DoubleVar(value=self._safe_float(self.ctx.cfg_val("auto_reprice_custom_offset_value", 0.0)))
        parent.add(self.frame, text="🤖 自动化")
        self._build_ui()
        self.ctx.automation_log = lambda msg: self.frame.after(0, lambda m=msg: self._log(m))
        try:
            self._event_target.bind("<<ImportedStoreUpdated>>", lambda _event: self._handle_imported_store_updated(), add="+")
            self._event_target.bind("<<ZhuanzhuanImportedStoreUpdated>>", lambda _event: self._handle_imported_store_updated(), add="+")
        except Exception:
            pass
        self._refresh_import_status()
        self._refresh_preview()
        self._refresh_status()

    def _build_ui(self):
        erp_frame = ttk.LabelFrame(self.frame, text="ERP 配置")
        erp_frame.pack(fill="x", padx=8, pady=6)

        erp_fields = [
            ("token", "Authorization Token", True),
            ("version", "Version", False),
        ]
        self._erp_vars: dict[str, tk.StringVar] = {}
        for row, (key, label, is_secret) in enumerate(erp_fields):
            ttk.Label(erp_frame, text=f"{label}:").grid(row=row, column=0, padx=6, pady=4, sticky="e")
            var = tk.StringVar(value=getattr(self.ctx.erp_config, key, "") or "")
            self._erp_vars[key] = var
            ttk.Entry(
                erp_frame,
                textvariable=var,
                width=46,
                show="*" if is_secret else "",
            ).grid(row=row, column=1, padx=6, pady=4, sticky="ew")
        erp_frame.columnconfigure(1, weight=1)
        ttk.Button(erp_frame, text="💾 保存 ERP 配置", command=self._save_erp_config).grid(
            row=len(erp_fields), column=1, sticky="e", padx=6, pady=(4, 6)
        )
        ttk.Label(
            erp_frame,
            text="请填写旧版爱管机 Authorization Token；Version 可留空，系统会按当天自动补默认值。",
            foreground="gray",
        ).grid(row=len(erp_fields) + 1, column=0, columnspan=2, padx=8, pady=(0, 6), sticky="w")

        import_frame = ttk.LabelFrame(self.frame, text="导入商品管理")
        import_frame.pack(fill="x", padx=8, pady=6)

        import_bar = ttk.Frame(import_frame)
        import_bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(import_bar, text="📥 从 ERP 同步并导入", command=self._sync_erp_import).pack(side="left", padx=2)
        ttk.Button(import_bar, text="🗂️ 打开商品管理", command=self._open_imported_window).pack(side="left", padx=2)
        ttk.Button(import_bar, text="🔄 刷新导入状态", command=self._refresh_import_status).pack(side="left", padx=2)
        ttk.Button(import_bar, text="🧪 刷新调价预览", command=self._refresh_preview).pack(side="left", padx=2)

        self._import_status_var = tk.StringVar(value="商品管理范围：未导入商品")
        ttk.Label(import_frame, textvariable=self._import_status_var, foreground="gray").pack(anchor="w", padx=8, pady=(0, 4))
        ttk.Label(
            import_frame,
            text="自动调价、滞销降价、未上架自动上架和商品管理窗口都只会处理这里导入的商品。",
            foreground="gray",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        ctrl_frame = ttk.LabelFrame(self.frame, text="调度器")
        ctrl_frame.pack(fill="x", padx=8, pady=6)

        self._scheduler_running = tk.BooleanVar(value=self.ctx.scheduler.is_running())
        ttk.Checkbutton(
            ctrl_frame,
            text="调度器运行中",
            variable=self._scheduler_running,
            command=self._toggle_scheduler,
        ).pack(side="left", padx=8, pady=4)
        ttk.Button(ctrl_frame, text="🔄 刷新状态", command=self._refresh_status).pack(side="left", padx=4)

        tasks_frame = ttk.LabelFrame(self.frame, text="自动化模块")
        tasks_frame.pack(fill="x", padx=8, pady=4)

        tasks_config = [
            ("auto_reprice", "模块1：自动调价（仅导入商品）", "auto_reprice_interval", 120),
            ("stale_drop", "模块2：滞销降价（仅导入商品）", "auto_reprice_interval", 120),
            ("auto_list", "模块3：未上架自动上架（仅导入商品）", "auto_list_interval", 120),
            ("sales_report", "模块4：导入状态播报（仅导入商品）", "sales_report_interval", 60),
        ]
        self._task_enabled: dict[str, tk.BooleanVar] = {}
        self._task_interval: dict[str, tk.IntVar] = {}

        for row, (task_name, label, interval_key, fallback_interval) in enumerate(tasks_config):
            task_info = self.ctx.scheduler.get_task(task_name) or {}
            enabled_var = tk.BooleanVar(value=bool(task_info.get("enabled", False)))
            self._task_enabled[task_name] = enabled_var

            ttk.Checkbutton(
                tasks_frame,
                text=label,
                variable=enabled_var,
                command=lambda n=task_name, v=enabled_var: self._toggle_task_enabled(n, v.get()),
            ).grid(row=row, column=0, sticky="w", padx=8, pady=3)

            ttk.Label(tasks_frame, text="间隔(分钟):").grid(row=row, column=1, padx=4)
            interval_var = tk.IntVar(value=int(self.ctx.cfg_val(interval_key, fallback_interval)))
            self._task_interval[task_name] = interval_var
            ttk.Spinbox(tasks_frame, from_=1, to=1440, textvariable=interval_var, width=6).grid(row=row, column=2, padx=4)
            ttk.Button(
                tasks_frame,
                text="应用",
                command=lambda n=task_name, k=interval_key, v=interval_var: self._apply_interval(n, k, v.get()),
            ).grid(row=row, column=3, padx=4)
            ttk.Button(
                tasks_frame,
                text="▶ 立即执行",
                command=lambda n=task_name: self._run_now(n),
            ).grid(row=row, column=4, padx=4)

        explain_frame = ttk.LabelFrame(self.frame, text="模块说明")
        explain_frame.pack(fill="x", padx=8, pady=4)
        ttk.Label(
            explain_frame,
            justify="left",
            foreground="gray",
            wraplength=1160,
            text=(
                "• 模块1 自动调价：只处理导入商品管理中的商品，先走系统建议价与启用规则，再叠加价格微调，最终执行改价。\n"
                "• 模块2 滞销降价：只处理导入商品管理中在售的商品，按滞销阶段决定是否降到保守价或继续按比例下调。\n"
                "• 模块3 未上架自动上架：只处理导入商品管理中符合资格且当前未上架的商品。\n"
                "• 模块4 导入状态播报：仅播报导入商品的状态变更（如已售、质检中→未上架），不直接修改商品价格或状态。"
            ),
        ).pack(fill="x", padx=8, pady=6)

        preview_frame = ttk.LabelFrame(self.frame, text="下次系统调价预览 / 价格微调")
        preview_frame.pack(fill="x", padx=8, pady=4)

        offset_bar = ttk.Frame(preview_frame)
        offset_bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(offset_bar, text="价格微调方式:").pack(side="left")
        ttk.Combobox(
            offset_bar,
            textvariable=self._custom_offset_mode_var,
            state="readonly",
            width=10,
            values=("off", "fixed", "percent"),
        ).pack(side="left", padx=(4, 8))
        ttk.Label(offset_bar, text="微调数值:").pack(side="left")
        ttk.Entry(offset_bar, textvariable=self._custom_offset_value_var, width=10).pack(side="left", padx=(4, 8))
        ttk.Button(offset_bar, text="💾 应用微调设置", command=self._save_custom_offset).pack(side="left", padx=2)
        ttk.Button(offset_bar, text="↺ 恢复系统建议价", command=self._reset_custom_offset).pack(side="left", padx=2)
        ttk.Label(
            offset_bar,
            text="off=关闭；fixed=固定加减金额；percent=按系统建议价百分比加减。",
            foreground="gray",
        ).pack(side="left", padx=10)

        self._preview_text = tk.Text(preview_frame, height=7, wrap="word", state="disabled", font=("Consolas", 9))
        self._preview_text.pack(fill="x", padx=6, pady=(2, 6))

        log_frame = ttk.LabelFrame(self.frame, text="运行日志")
        log_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self._log_text = tk.Text(log_frame, height=10, state="disabled", font=("Consolas", 9))
        vsb = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.config(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True, padx=4, pady=4)

        ttk.Button(log_frame, text="🗑️ 清空日志", command=self._clear_log).pack(anchor="e", padx=4)

        wx_frame = ttk.LabelFrame(self.frame, text="企业微信配置")
        wx_frame.pack(fill="x", padx=8, pady=4)

        wx_fields = [
            ("corp_id", "企业ID", "entry"),
            ("corp_secret", "应用Secret", "entry"),
            ("agent_id", "AgentID", "entry"),
            ("robot_webhook", "机器人Webhook", "entry"),
        ]
        self._wx_vars: dict[str, tk.StringVar] = {}
        for row, (key, label, _) in enumerate(wx_fields):
            ttk.Label(wx_frame, text=f"{label}:").grid(row=row, column=0, padx=4, pady=3, sticky="e")
            var = tk.StringVar(value=self.ctx.wxapp_config.get(key, ""))
            self._wx_vars[key] = var
            ttk.Entry(wx_frame, textvariable=var, width=46, show="*" if "secret" in key.lower() else "").grid(
                row=row, column=1, padx=4, sticky="ew"
            )
        wx_frame.columnconfigure(1, weight=1)
        ttk.Button(wx_frame, text="💾 保存配置", command=self._save_wx_config).grid(
            row=len(wx_fields), column=1, sticky="e", padx=4, pady=6
        )
        self._status_var = tk.StringVar()
        ttk.Label(self.frame, textvariable=self._status_var, foreground="gray").pack(pady=2)

    def _handle_imported_store_updated(self):
        self._refresh_import_status()
        self._refresh_preview()

    def _normalize_offset_mode(self, value) -> str:
        mode = str(value or "off").strip().lower()
        return mode if mode in {"off", "fixed", "percent"} else "off"

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _render_preview(self, text: str):
        self._preview_text.config(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("1.0", text)
        self._preview_text.config(state="disabled")

    def _preview_source_item(self):
        items = [
            item for item in self.ctx.zhuanzhuan.imported_store.get_all()
            if not getattr(item, "ignored", False)
        ]
        if not items:
            return None
        checked = [item for item in items if getattr(item, "selected", True)]
        return checked[0] if checked else items[0]

    def _refresh_preview(self):
        item = self._preview_source_item()
        if item is None:
            self._render_preview(
                "暂无导入商品，无法预览自动调价。\n\n"
                "先执行“从 ERP 同步并导入”或在商品管理窗口手动导入 IMEI / 质检码后，再回来查看预览。\n\n"
                "说明：\n"
                "- 自动调价 / 滞销降价 / 未上架自动上架都只处理导入商品\n"
                "- 预览不会扫描全店，只从当前导入商品中取样"
            )
            return

        mode = self._normalize_offset_mode(self._custom_offset_mode_var.get())
        value = self._safe_float(self._custom_offset_value_var.get(), 0.0)
        pipeline = run_reprice_pipeline(
            item,
            self.ctx.sold_cache,
            self.ctx.rule_engine,
            custom_mode=mode,
            custom_value=value,
            apply_rules=True,
        )
        decision = pipeline["decision"]
        preview = pipeline["preview"]
        explain_lines = decision.get("explain_lines") or ["无启用规则"]
        final_price = pipeline.get("suggested_price")
        system_price = decision.get("system_price")
        final_settle = pipeline.get("suggested_settle_price")
        diff_text = "—"
        if final_price is not None:
            diff_text = f"{final_price - item.current_price:+.0f}"

        lines = [
            "处理范围：",
            "- 自动调价：仅导入商品",
            "- 滞销降价：仅导入商品",
            "- 未上架自动上架：仅导入商品中符合资格且当前未上架的商品",
            "",
            f"预览样本：{item.qc_code or item.product_id} / {item.title or item.model or '—'}",
            f"账号：{item.account_name or '—'}",
            f"当前价：¥{item.current_price:,.0f}",
            f"系统建议价：{'¥%s' % format(system_price, ',.0f') if system_price is not None else '—'}",
            f"价格微调：{decision.get('custom_summary', '关闭')}",
            f"最终执行价：{'¥%s' % format(final_price, ',.0f') if final_price is not None else '—'}",
            f"建议调价：{diff_text if final_price is not None else '—'} 元",
            f"本次最终会从 {'¥%s' % format(item.current_price, ',.0f')} 改到 {'¥%s' % format(final_price, ',.0f') if final_price is not None else '—'}",
            f"预计结算价：{'¥%s' % format(final_settle, ',.0f') if final_settle is not None else '—'}",
            f"相对当前差价：{diff_text}",
            f"命中规则：{decision.get('rule_hit') or '未命中，使用基础建议价'}",
            "",
            "规则说明：",
            *explain_lines,
            "",
            "摘要：",
            preview,
            "",
            "提示：当前预览按上方价格微调设置即时计算；点击“应用微调设置”后，自动调价任务会按相同口径执行。",
        ]
        self._render_preview("\n".join(lines))

    def _refresh_import_status(self):
        count = self.ctx.zhuanzhuan.imported_store.count()
        text = f"商品管理范围：已导入 {count} 件商品"
        if self._last_import_summary:
            text += f" | 最近一次同步：{self._last_import_summary}"
        else:
            text += " | 自动化任务只处理这些商品"
        self._import_status_var.set(text)

    def _toggle_scheduler(self):
        if self._scheduler_running.get():
            self.ctx.scheduler.start()
            self._log("调度器已启动")
        else:
            self.ctx.scheduler.stop()
            self._log("调度器已停止")

    def _toggle_task_enabled(self, task_name: str, enabled: bool):
        self.ctx.scheduler.set_enabled(task_name, enabled)
        self._log(f"{task_name} 已{'开启' if enabled else '关闭'}")
        self._refresh_status()

    def _apply_interval(self, task_name: str, interval_key: str, minutes: int):
        minutes = max(1, int(minutes or 1))
        self.ctx.scheduler.set_interval(task_name, minutes)
        cfg.set(interval_key, minutes)
        self._log(f"{task_name} 间隔已更新为 {minutes} 分钟")
        self._refresh_status()

    def _save_custom_offset(self):
        mode = self._normalize_offset_mode(self._custom_offset_mode_var.get())
        value = self._safe_float(self._custom_offset_value_var.get(), 0.0)
        self._custom_offset_mode_var.set(mode)
        self._custom_offset_value_var.set(value)
        cfg.set("auto_reprice_custom_offset_mode", mode)
        cfg.set("auto_reprice_custom_offset_value", value)
        self._log(f"自动调价价格微调设置已保存：{mode} / {value}")
        self._refresh_preview()

    def _reset_custom_offset(self):
        self._custom_offset_mode_var.set("off")
        self._custom_offset_value_var.set(0.0)
        self._save_custom_offset()

    def _refresh_status(self):
        self._scheduler_running.set(self.ctx.scheduler.is_running())
        status = self.ctx.scheduler.get_status()
        parts = []
        for name, info in status.items():
            if name in self._task_enabled:
                self._task_enabled[name].set(info["enabled"])
            state = "✅" if info["enabled"] else "⬜"
            last = info["last_run"] or "从未"
            parts.append(f"{state} {name}: 运行{info['run_count']}次 最后:{last[:16]}")
        self._status_var.set("  |  ".join(parts))
        self.frame.after(30000, self._refresh_status)

    def _run_now(self, task_name: str):
        self._log(f"手动触发: {task_name}")
        self.ctx.scheduler.run_now(task_name)

    def _save_erp_config(self):
        for key, var in self._erp_vars.items():
            self.ctx.erp_config.set(key, var.get().strip())
        self._log("ERP 配置已保存")
        messagebox.showinfo("保存", "ERP 配置已保存")

    def _sync_erp_import(self):
        if self._erp_sync_running:
            return
        self._erp_sync_running = True
        self._log("开始从 ERP 同步并导入商品 …")
        threading.Thread(target=self._do_sync_erp_import, daemon=True).start()

    def _do_sync_erp_import(self):
        from ..automation.tasks import task_erp_sync

        result = task_erp_sync(
            self.ctx.erp_config,
            self.ctx.cost_map,
            self.ctx.account_store,
            imported_store=self.ctx.zhuanzhuan.imported_store,
            on_progress=lambda msg: self.frame.after(0, lambda m=msg: self._log(m)),
            sold_cache=self.ctx.sold_cache,
            sold_sync_days=30,
        )
        summary = result.get("summary", str(result)) if isinstance(result, dict) else str(result)
        self.frame.after(0, lambda s=summary: self._after_sync_erp_import(s))

    def _after_sync_erp_import(self, summary: str):
        self._erp_sync_running = False
        self._last_import_summary = summary
        self._refresh_import_status()
        self._refresh_preview()
        self._log(f"ERP 导入完成: {summary}")
        try:
            self._event_target.event_generate("<<ImportedStoreUpdated>>")
        except Exception:
            pass

    def _open_imported_window(self):
        if self._manage_window is not None:
            try:
                if self._manage_window.winfo_exists():
                    self._manage_window.lift()
                    self._manage_window.focus_force()
                    return
            except Exception:
                self._manage_window = None

        from .windows.imported_products_window import ImportedProductsWindow

        self._manage_window = ImportedProductsWindow(
            self.frame.winfo_toplevel(),
            self.ctx,
            event_target=self._event_target,
            on_close=self._on_manage_window_close,
        )

    def _on_manage_window_close(self):
        self._manage_window = None
        self._refresh_import_status()
        self._refresh_preview()

    def _save_wx_config(self):
        for key, var in self._wx_vars.items():
            self.ctx.wxapp_config.set(key, var.get())
        webhook = self._wx_vars.get("robot_webhook", tk.StringVar()).get()
        self.ctx.robot_notifier.webhook_url = webhook
        messagebox.showinfo("保存", "企业微信配置已保存")

    def _log(self, msg: str):
        import datetime
        self._log_text.config(state="normal")
        self._log_text.insert("end", f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")
