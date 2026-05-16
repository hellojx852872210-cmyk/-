# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import re
import threading
from typing import List, Optional

from ..config import BROWSER_INSTANCES_FILE, JsonConfigFile, LEGACY_PATHS


@dataclass
class BrowserInstance:
    instance_id: str
    platform: str
    name: str
    profile_key: str
    note: str = ""
    is_default: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def touch(self):
        self.updated_at = datetime.now().isoformat(timespec="seconds")


class BrowserInstanceStore:
    def __init__(self, path: str = BROWSER_INSTANCES_FILE):
        legacy_path = None if path != BROWSER_INSTANCES_FILE else LEGACY_PATHS.get("browser_instances")
        self._file = JsonConfigFile(path, legacy_path)
        self.path = self._file.path
        self._lock = threading.RLock()
        self._instances: List[BrowserInstance] = []
        self.load()

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _normalize_text(self, value: str) -> str:
        return str(value or "").strip()

    def _slugify(self, value: str) -> str:
        text = self._normalize_text(value).lower()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        return text.strip("-") or "browser"

    def _next_instance_id(self, platform: str, name: str) -> str:
        base = self._slugify(name)
        existing = {item.instance_id for item in self._instances if item.platform == platform}
        candidate = base
        counter = 2
        while candidate in existing:
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def _profile_key(self, platform: str, page_key: str, instance_id: str) -> str:
        return f"{platform}-{page_key}-{instance_id}"

    def _instance_from_dict(self, data: dict) -> BrowserInstance:
        platform = self._normalize_text(data.get("platform", "")) or "zhuanzhuan"
        instance_id = self._normalize_text(data.get("instance_id", "")) or self._next_instance_id(platform, data.get("name", "browser"))
        profile_key = self._normalize_text(data.get("profile_key", "")) or self._profile_key(platform, "fetch", instance_id)
        now = self._now()
        return BrowserInstance(
            instance_id=instance_id,
            platform=platform,
            name=self._normalize_text(data.get("name", "")) or instance_id,
            profile_key=profile_key,
            note=self._normalize_text(data.get("note", "")),
            is_default=bool(data.get("is_default", False)),
            created_at=self._normalize_text(data.get("created_at", "")) or now,
            updated_at=self._normalize_text(data.get("updated_at", "")) or now,
        )

    def _save_locked(self) -> None:
        self._file.save([asdict(item) for item in self._instances])
        self.path = self._file.path

    def _clone(self, instance: BrowserInstance) -> BrowserInstance:
        return self._instance_from_dict(asdict(instance))

    def _ensure_default_locked(self, platform: str) -> None:
        platform_items = [item for item in self._instances if item.platform == platform]
        if not platform_items:
            return
        default_seen = False
        for item in platform_items:
            if item.is_default and not default_seen:
                default_seen = True
                continue
            if item.is_default and default_seen:
                item.is_default = False
        if not default_seen:
            platform_items[0].is_default = True

    def load(self) -> List[BrowserInstance]:
        with self._lock:
            raw = self._file.load(list)
            self.path = self._file.path
            self._instances = [self._instance_from_dict(item) for item in raw if isinstance(item, dict)]
            platforms = {item.platform for item in self._instances}
            for platform in platforms:
                self._ensure_default_locked(platform)
            return self.list_all()

    def list_all(self, platform: Optional[str] = None) -> List[BrowserInstance]:
        with self._lock:
            items = self._instances
            if platform:
                items = [item for item in items if item.platform == platform]
            return [self._clone(item) for item in items]

    def get(self, platform: str, instance_id: str) -> Optional[BrowserInstance]:
        with self._lock:
            for item in self._instances:
                if item.platform == platform and item.instance_id == instance_id:
                    return self._clone(item)
            return None

    def get_default(self, platform: str, *, create: bool = False, default_name: str = "默认浏览器") -> Optional[BrowserInstance]:
        with self._lock:
            for item in self._instances:
                if item.platform == platform and item.is_default:
                    return self._clone(item)
            platform_items = [item for item in self._instances if item.platform == platform]
            if platform_items:
                platform_items[0].is_default = True
                self._save_locked()
                return self._clone(platform_items[0])
        if create:
            return self.create(platform=platform, name=default_name, is_default=True)
        return None

    def create(self, *, platform: str, name: str, note: str = "", is_default: bool = False) -> BrowserInstance:
        platform = self._normalize_text(platform) or "zhuanzhuan"
        name = self._normalize_text(name) or "新浏览器"
        with self._lock:
            instance_id = self._next_instance_id(platform, name)
            instance = BrowserInstance(
                instance_id=instance_id,
                platform=platform,
                name=name,
                profile_key=self._profile_key(platform, "fetch", instance_id),
                note=self._normalize_text(note),
                is_default=is_default or not any(item.platform == platform for item in self._instances),
            )
            if instance.is_default:
                for item in self._instances:
                    if item.platform == platform:
                        item.is_default = False
            self._instances.append(instance)
            self._ensure_default_locked(platform)
            self._save_locked()
            return self._clone(instance)

    def rename(self, platform: str, instance_id: str, name: str) -> Optional[BrowserInstance]:
        name = self._normalize_text(name)
        if not name:
            return None
        with self._lock:
            for item in self._instances:
                if item.platform == platform and item.instance_id == instance_id:
                    item.name = name
                    item.touch()
                    self._save_locked()
                    return self._clone(item)
            return None

    def set_default(self, platform: str, instance_id: str) -> Optional[BrowserInstance]:
        with self._lock:
            target = None
            for item in self._instances:
                if item.platform != platform:
                    continue
                item.is_default = item.instance_id == instance_id
                if item.is_default:
                    target = item
            if target is None:
                return None
            target.touch()
            self._ensure_default_locked(platform)
            self._save_locked()
            return self._clone(target)

    def delete(self, platform: str, instance_id: str) -> bool:
        with self._lock:
            before = len(self._instances)
            self._instances = [item for item in self._instances if not (item.platform == platform and item.instance_id == instance_id)]
            if len(self._instances) == before:
                return False
            self._ensure_default_locked(platform)
            self._save_locked()
            return True
