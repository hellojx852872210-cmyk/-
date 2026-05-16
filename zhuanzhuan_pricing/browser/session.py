# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class BrowserSession:
    profile_key: str
    profile_factory: Callable[[], object]
    view_factory: Callable[[], object]

    def profile(self):
        return self.profile_factory()

    def create_view(self):
        return self.view_factory()


class BrowserSessionRegistry:
    def __init__(self, manager):
        self.manager = manager
        self._sessions: dict[str, BrowserSession] = {}

    def session(self, profile_key: str) -> BrowserSession:
        if profile_key not in self._sessions:
            self._sessions[profile_key] = BrowserSession(
                profile_key=profile_key,
                profile_factory=lambda key=profile_key: self.manager.profile(key),
                view_factory=lambda key=profile_key: self.manager.create_view(key),
            )
        return self._sessions[profile_key]


class PlatformBrowserContext:
    def __init__(self, registry: BrowserSessionRegistry, platform: str, page_keys: tuple[str, ...]):
        self._registry = registry
        self._platform = platform
        self._page_keys = page_keys
        for page_key in page_keys:
            setattr(self, page_key, registry.session(f"{platform}-{page_key}"))

    def session(self, profile_key: str) -> BrowserSession:
        return self._registry.session(profile_key)

    def session_for_instance(self, page_key: str, instance_id: str) -> BrowserSession:
        return self.session(f"{self._platform}-{page_key}-{instance_id}")

    def items(self):
        for page_key in self._page_keys:
            yield page_key, getattr(self, page_key)
