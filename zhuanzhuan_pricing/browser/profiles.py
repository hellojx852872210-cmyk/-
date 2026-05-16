# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from ..config import paths


class BrowserProfilePaths:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or (paths.data_dir / "browser_profiles"))

    def profile_dir(self, profile_key: str) -> Path:
        return self.root / profile_key

    def storage_dir(self, profile_key: str) -> Path:
        return self.profile_dir(profile_key) / "storage"

    def cache_dir(self, profile_key: str) -> Path:
        return self.profile_dir(profile_key) / "cache"

    def ensure_profile_dirs(self, profile_key: str) -> tuple[Path, Path, Path]:
        profile_dir = self.profile_dir(profile_key)
        storage_dir = self.storage_dir(profile_key)
        cache_dir = self.cache_dir(profile_key)
        profile_dir.mkdir(parents=True, exist_ok=True)
        storage_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir, storage_dir, cache_dir
