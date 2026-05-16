# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from .profiles import BrowserProfilePaths


class BrowserProfileManager:
    def __init__(self, root: Path | None = None):
        self.paths = BrowserProfilePaths(root)
        self._profiles: dict[str, object] = {}

    def _persistent_cookie_policy(self, profile_cls):
        enum_owner = getattr(profile_cls, "PersistentCookiesPolicy", profile_cls)
        return getattr(enum_owner, "ForcePersistentCookies", getattr(profile_cls, "ForcePersistentCookies", None))

    def profile(self, profile_key: str):
        if profile_key in self._profiles:
            return self._profiles[profile_key]

        from PySide6.QtWebEngineCore import QWebEngineProfile

        _profile_dir, storage_dir, cache_dir = self.paths.ensure_profile_dirs(profile_key)
        profile = QWebEngineProfile(profile_key)
        profile.setPersistentStoragePath(str(storage_dir))
        profile.setCachePath(str(cache_dir))
        policy = self._persistent_cookie_policy(QWebEngineProfile)
        if policy is not None:
            profile.setPersistentCookiesPolicy(policy)
        profile.setHttpUserAgent(profile.httpUserAgent() + " ZhuanzhuanPricingQt/0.1")
        self._profiles[profile_key] = profile
        return profile

    def create_view(self, profile_key: str):
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView

        view = QWebEngineView()
        page = QWebEnginePage(self.profile(profile_key), view)
        view.setPage(page)
        return view
