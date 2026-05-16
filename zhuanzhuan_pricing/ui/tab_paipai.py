# -*- coding: utf-8 -*-
"""
拍拍平台 Tab
- 配置保存
- 冒烟检查
- 请求预览
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from ..services.paipai_service import PAIPAI_APP_KEY, PAIPAI_LIST_API_NAME

if TYPE_CHECKING:
    from .app import AppContext


class PaipaiTab:
    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="🛍️ 拍拍")
        self._build_ui()

    def _build_ui(self):
        frame = ttk.LabelFrame(self.frame, text="拍拍配置 / 冒烟检查")
        frame.pack(fill="x", padx=12, pady=10)

        fields = [
            ("cookie", "Cookie", True),
            ("sign", "Sign", True),
            ("body", "列表 Body", False),
        ]
        self._vars: dict[str, tk.StringVar] = {}
        for row, (key, label, is_secret) in enumerate(fields):
            ttk.Label(frame, text=f"{label}:").grid(row=row, column=0, padx=6, pady=4, sticky="e")
            var = tk.StringVar(value=self.ctx.paipai_config.get(key, "") or "")
            self._vars[key] = var
            ttk.Entry(
                frame,
                textvariable=var,
                width=84,
                show="*" if is_secret else "",
            ).grid(row=row, column=1, padx=6, pady=4, sticky="ew")

        meta_row = len(fields)
        ttk.Label(frame, text="AppKey:").grid(row=meta_row, column=0, padx=6, pady=4, sticky="e")
        self._app_key_var = tk.StringVar(value=self.ctx.paipai_config.get("app_key", PAIPAI_APP_KEY) or PAIPAI_APP_KEY)
        ttk.Entry(frame, textvariable=self._app_key_var, width=36).grid(row=meta_row, column=1, padx=6, pady=4, sticky="w")

        ttk.Label(frame, text="ApiName:").grid(row=meta_row + 1, column=0, padx=6, pady=4, sticky="e")
        self._api_name_var = tk.StringVar(value=self.ctx.paipai_config.get("api_name", PAIPAI_LIST_API_NAME) or PAIPAI_LIST_API_NAME)
        ttk.Entry(frame, textvariable=self._api_name_var, width=56).grid(row=meta_row + 1, column=1, padx=6, pady=4, sticky="w")

        btn_bar = ttk.Frame(frame)
        btn_bar.grid(row=meta_row + 2, column=1, padx=6, pady=(6, 8), sticky="w")
        ttk.Button(btn_bar, text="💾 保存拍拍配置", command=self._save_config).pack(side="left", padx=(0, 6))
        ttk.Button(btn_bar, text="🧪 冒烟检查", command=self._smoke_check).pack(side="left", padx=(0, 6))
        ttk.Button(btn_bar, text="🧱 预览请求", command=self._preview_request).pack(side="left")

        self._status_var = tk.StringVar(value="拍拍未检查")
        ttk.Label(frame, textvariable=self._status_var, foreground="gray").grid(
            row=meta_row + 3, column=0, columnspan=2, padx=8, pady=(0, 6), sticky="w"
        )
        ttk.Label(
            frame,
            text="当前先接入列表请求骨架；若缺 body/sign，会给出受控失败提示。",
            foreground="gray",
        ).grid(row=meta_row + 4, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="w")
        frame.columnconfigure(1, weight=1)

        preview_frame = ttk.LabelFrame(self.frame, text="当前请求预览")
        preview_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        self._preview_text = tk.Text(preview_frame, height=18, wrap="word", state="disabled", font=("Consolas", 10))
        self._preview_text.pack(fill="both", expand=True, padx=8, pady=8)
        self._render_preview("点击“预览请求”后，这里会显示当前将要发送的 URL、表单字段和配置状态。")

    def _render_preview(self, text: str):
        self._preview_text.config(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("1.0", text)
        self._preview_text.config(state="disabled")

    def _save_config(self):
        for key, var in self._vars.items():
            self.ctx.paipai_config.set(key, var.get().strip())
        self.ctx.paipai_config.set("app_key", self._app_key_var.get().strip() or PAIPAI_APP_KEY)
        self.ctx.paipai_config.set("api_name", self._api_name_var.get().strip() or PAIPAI_LIST_API_NAME)
        self.ctx.paipai_client.refresh_config()
        self._status_var.set(f"拍拍配置已保存：{self.ctx.paipai_config.path}")
        messagebox.showinfo("保存", "拍拍配置已保存")

    def _smoke_check(self):
        self.ctx.paipai_client.refresh_config()
        ok, message = self.ctx.paipai_client.health_check()
        if ok:
            request_spec = self.ctx.paipai_client.build_list_request()
            self._status_var.set(f"请求已就绪: POST {request_spec['url'][:160]}")
            messagebox.showinfo("拍拍冒烟检查", f"通过\n\n{message}")
            return
        self._status_var.set(f"拍拍冒烟检查未通过：{message}")
        messagebox.showwarning("拍拍冒烟检查", message)

    def _preview_request(self):
        self.ctx.paipai_client.refresh_config()
        request_spec = self.ctx.paipai_client.build_list_request()
        body_keys = ", ".join(request_spec["data"].keys()) or "无"
        message = (
            f"URL:\n{request_spec['url']}\n\n"
            f"表单字段: {body_keys}\n"
            f"Cookie已配置: {'是' if bool(self.ctx.paipai_config.cookie) else '否'}\n"
            f"Sign已配置: {'是' if bool(self.ctx.paipai_config.sign) else '否'}\n"
            f"Body已配置: {'是' if bool(self.ctx.paipai_config.body) else '否'}\n"
            f"配置文件: {self.ctx.paipai_config.path}"
        )
        self._status_var.set("已生成拍拍请求预览")
        self._render_preview(message)
        messagebox.showinfo("拍拍请求预览", message)
