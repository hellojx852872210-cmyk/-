# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Optional

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtNetwork import QNetworkCookie

from ..browser.manager import BrowserProfileManager
from ..browser.session import BrowserSessionRegistry
from ..core.models import Account
from .browser_instance_store import BrowserInstance, BrowserInstanceStore
from .data_store import AccountStore
from .paipai_service import PaipaiClient, PaipaiConfig
from .zhuanzhuan_api import ImeiService


class _CookieCollector(QObject):
    cookie_received = Signal(object)

    def __init__(self):
        super().__init__()
        self.cookies = []

    def on_cookie_added(self, cookie):
        self.cookies.append(cookie)
        self.cookie_received.emit(cookie)


class ShopCookieService:
    def __init__(
        self,
        *,
        account_store: AccountStore | None = None,
        instance_store: BrowserInstanceStore | None = None,
        profile_manager: BrowserProfileManager | None = None,
        session_registry: BrowserSessionRegistry | None = None,
        paipai_config: PaipaiConfig | None = None,
        paipai_client: PaipaiClient | None = None,
    ):
        self.account_store = account_store or AccountStore()
        self.instance_store = instance_store or BrowserInstanceStore()
        self.profile_manager = profile_manager or BrowserProfileManager()
        self.session_registry = session_registry or BrowserSessionRegistry(self.profile_manager)
        self.paipai_config = paipai_config or PaipaiConfig()
        self.paipai_client = paipai_client or PaipaiClient(self.paipai_config)

    def platform_label(self, platform: str) -> str:
        return "拍拍" if platform == "paipai" else "转转"

    def normalize_platform(self, platform: str) -> str:
        text = str(platform or "").strip().lower()
        return "paipai" if text == "paipai" else "zhuanzhuan"

    def list_instances(self, platform: str) -> list[BrowserInstance]:
        return self.instance_store.list_all(platform=self.normalize_platform(platform))

    def get_or_create_default_instance(self, platform: str) -> BrowserInstance:
        instance = self.instance_store.get_default(self.normalize_platform(platform), create=True)
        if instance is None:
            raise RuntimeError("无法创建默认浏览器实例")
        return instance

    def create_instance(self, platform: str, name: str, note: str = "") -> BrowserInstance:
        return self.instance_store.create(platform=self.normalize_platform(platform), name=name, note=note)

    def set_default_instance(self, platform: str, instance_id: str) -> BrowserInstance:
        updated = self.instance_store.set_default(self.normalize_platform(platform), instance_id)
        if updated is None:
            raise ValueError(f"浏览器实例不存在: {instance_id}")
        return updated

    def get_instance(self, platform: str, instance_id: str) -> BrowserInstance | None:
        return self.instance_store.get(self.normalize_platform(platform), instance_id)

    def session_for_instance(self, platform: str, instance_id: str):
        instance = self.get_instance(platform, instance_id)
        if instance is None:
            raise ValueError(f"浏览器实例不存在: {instance_id}")
        return self.session_registry.session(instance.profile_key)

    def create_browser_view_for_instance(self, platform: str, instance_id: str):
        session = self.session_for_instance(platform, instance_id)
        return session.create_view()

    def format_cookie_pair(self, cookie) -> str:
        if not isinstance(cookie, QNetworkCookie):
            return ""
        name = bytes(cookie.name()).decode("utf-8", errors="ignore").strip()
        value = bytes(cookie.value()).decode("utf-8", errors="ignore").strip()
        if not name:
            return ""
        return f"{name}={value}"

    def collect_runtime_cookie_pairs(self, platform: str, instance_id: str) -> list[str]:
        session = self.session_for_instance(platform, instance_id)
        profile = session.profile()
        cookie_store = profile.cookieStore()
        collector = _CookieCollector()
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        def _restart(_cookie=None):
            timer.start(200)

        collector.cookie_received.connect(_restart)
        cookie_store.cookieAdded.connect(collector.on_cookie_added)
        try:
            cookie_store.loadAllCookies()
            timer.start(200)
            loop.exec()
        finally:
            try:
                collector.cookie_received.disconnect(_restart)
            except Exception:
                pass
            try:
                cookie_store.cookieAdded.disconnect(collector.on_cookie_added)
            except Exception:
                pass

        pairs: list[str] = []
        seen = set()
        for cookie in collector.cookies:
            pair = self.format_cookie_pair(cookie)
            if not pair or pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
        return pairs

    def collect_persistent_cookie_pairs(self, platform: str, instance_id: str) -> list[str]:
        instance = self.get_instance(platform, instance_id)
        if instance is None:
            return []
        profile_dir = self.profile_manager.paths.profile_dir(instance.profile_key)
        cookie_db = profile_dir / "storage" / "Cookies"
        if not cookie_db.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute("select host_key, name, value from cookies order by host_key, name").fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

        pairs = []
        seen = set()
        for _host, name, value in rows:
            name = str(name or "").strip()
            value = str(value or "").strip()
            if not name:
                continue
            pair = f"{name}={value}"
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
        return pairs

    def export_cookie(self, platform: str, instance_id: str) -> tuple[str, str]:
        pairs = self.collect_runtime_cookie_pairs(platform, instance_id)
        source = "runtime"
        if not pairs:
            pairs = self.collect_persistent_cookie_pairs(platform, instance_id)
            source = "persistent"
        return "; ".join(pairs), source

    def list_accounts(self, platform: Optional[str] = None, group: Optional[str] = None) -> list[Account]:
        platform = self.normalize_platform(platform or "") if platform else None
        group_filter = str(group or "").strip().lower()
        rows = []
        for account in self.account_store.load_all():
            if platform and account.platform != platform:
                continue
            if group_filter and group_filter not in (account.group or "default").lower():
                continue
            rows.append(account)
        return rows

    def save_account(
        self,
        *,
        name: str,
        cookie: str,
        platform: str,
        group: str = "default",
        browser_instance_id: str = "",
        note: str = "",
        enabled: bool = True,
    ) -> Account:
        platform = self.normalize_platform(platform)
        name = str(name or "").strip()
        cookie = str(cookie or "").strip()
        if not name:
            raise ValueError("账号名称不能为空")
        if not cookie:
            raise ValueError("Cookie 不能为空")

        account = Account(
            name=name,
            cookie=cookie,
            note=str(note or "").strip(),
            enabled=bool(enabled),
            platform=platform,
            group=str(group or "default").strip() or "default",
            browser_instance_id=str(browser_instance_id or "").strip(),
        )

        accounts = self.account_store.load_all()
        replaced = False
        for index, existing in enumerate(accounts):
            if existing.name == account.name and existing.platform == account.platform:
                accounts[index] = account
                replaced = True
                break
        if not replaced:
            accounts.append(account)
        self.account_store.save_all(accounts)
        return account

    def apply_cookie_from_instance(
        self,
        *,
        name: str,
        platform: str,
        instance_id: str,
        group: Optional[str] = None,
    ) -> Account:
        cookie_text, _source = self.export_cookie(platform, instance_id)
        if not cookie_text:
            raise ValueError("当前浏览器实例未读取到 Cookie")

        accounts = self.account_store.load_all()
        target = None
        for account in accounts:
            if account.name == name and account.platform == self.normalize_platform(platform):
                target = account
                break
        if target is None:
            raise ValueError(f"账号不存在: {name}")

        return self.save_account(
            name=target.name,
            cookie=cookie_text,
            platform=target.platform,
            group=group if group is not None else target.group,
            browser_instance_id=instance_id,
            note=target.note,
            enabled=target.enabled,
        )

    def validate_account(self, *, name: str, platform: str, cookie: Optional[str] = None) -> tuple[bool, str]:
        platform = self.normalize_platform(platform)
        cookie_text = str(cookie or "").strip()
        if not cookie_text:
            for account in self.account_store.load_all():
                if account.name == name and account.platform == platform:
                    cookie_text = account.cookie
                    break
        if not cookie_text:
            return False, "Cookie 为空"

        if platform == "paipai":
            self.paipai_config.load()
            if not self.paipai_config.sign:
                return True, "Cookie 已填写；未配置 sign（后续补齐）"
            if not self.paipai_config.body:
                return True, "Cookie 已填写；未配置 body（后续补齐）"
            ok, message = self.paipai_client.health_check()
            return ok, message

        service = ImeiService(name, cookie_text)
        ok, message = service.check_cookie_valid()
        return ok, (message or ("连接成功" if ok else "连接失败"))

    def validate_all(self, platform: Optional[str] = None) -> dict:
        rows = self.list_accounts(platform=platform)
        summary = {"total": 0, "ok": 0, "fail": 0, "details": []}
        for account in rows:
            summary["total"] += 1
            ok, message = self.validate_account(name=account.name, platform=account.platform, cookie=account.cookie)
            if ok:
                summary["ok"] += 1
            else:
                summary["fail"] += 1
            summary["details"].append(
                {
                    "name": account.name,
                    "platform": account.platform,
                    "group": account.group,
                    "ok": ok,
                    "message": message,
                }
            )
        return summary

    def account_to_dict(self, account: Account) -> dict:
        return asdict(account)
