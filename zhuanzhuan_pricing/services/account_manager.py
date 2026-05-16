"""
services/account_manager.py — 账号管理
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

from config import ACCOUNTS_FILE
from core.models import Account

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self, config_path: str = ACCOUNTS_FILE):
        self.config_path = config_path
        self.accounts: list[Account] = self._load()

    def _load(self) -> list[Account]:
        if not os.path.exists(self.config_path):
            return []
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return [Account(**item) for item in json.load(f)]
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("账号配置文件损坏，已重置: %s", e)
            return []

    def save(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump([asdict(a) for a in self.accounts], f,
                      ensure_ascii=False, indent=4)

    def add(self, name: str, cookie: str):
        if not name or not cookie:
            raise ValueError("店名和 Cookie 不能为空")
        if any(a.name == name for a in self.accounts):
            raise ValueError(f"店铺 '{name}' 已存在")
        self.accounts.append(Account(name=name, cookie=cookie))
        self.save()

    def update_cookie(self, name: str, new_cookie: str):
        for acc in self.accounts:
            if acc.name == name:
                acc.cookie = new_cookie
                self.save()
                return
        raise ValueError(f"账号 '{name}' 不存在")

    def remove(self, name: str):
        self.accounts = [a for a in self.accounts if a.name != name]
        self.save()

    def get_cookie(self, name: str) -> str:
        for a in self.accounts:
            if a.name == name:
                return a.cookie
        return ""

    @property
    def names(self) -> list[str]:
        return [a.name for a in self.accounts]
