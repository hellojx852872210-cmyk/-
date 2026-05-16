# -*- coding: utf-8 -*-
"""
全局配置：常量 + 用户可覆盖参数 + 统一路径/配置读写
"""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
_DATA_DIR = _PROJECT_ROOT / "data"
_RUNTIME_DIR = _PROJECT_ROOT / "runtime"


class ProjectPaths:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.config_dir = self.project_root / "config"
        self.data_dir = self.project_root / "data"
        self.runtime_dir = self.project_root / "runtime"

    @property
    def browser_instances_file(self) -> Path:
        return self.config_dir / "browser_instances.json"

    @property
    def paipai_config_file(self) -> Path:
        return self.config_dir / "paipai_config.json"

    @property
    def accounts_file(self) -> Path:
        return self.config_dir / "zhuanzhuan_accounts.json"

    @property
    def erp_config_file(self) -> Path:
        return self.config_dir / "aiguanji_config.json"

    @property
    def wxapp_config_file(self) -> Path:
        return self.config_dir / "wxwork_app_config.json"

    @property
    def rule_config_file(self) -> Path:
        return self.config_dir / "pricing_rules.json"

    @property
    def app_config_file(self) -> Path:
        return self.config_dir / "app_config.json"

    @property
    def sold_cache_file(self) -> Path:
        return self.data_dir / "zhuanzhuan_sold_cache.csv"

    @property
    def erp_cost_map_file(self) -> Path:
        return self.data_dir / "erp_cost_map.json"

    @property
    def price_history_db(self) -> Path:
        return self.data_dir / "price_history.db"

    @property
    def same_sale_file(self) -> Path:
        return self.data_dir / "same_sale_groups.json"


paths = ProjectPaths(_PROJECT_ROOT)

LEGACY_PATHS = {
    "accounts": _PROJECT_ROOT / "zhuanzhuan_accounts.json",
    "sold_cache": _PROJECT_ROOT / "zhuanzhuan_sold_cache.csv",
    "erp_config": _PROJECT_ROOT / "aiguanji_config.json",
    "erp_cost_map": _PROJECT_ROOT / "erp_cost_map.json",
    "wxapp_config": _PROJECT_ROOT / "wxwork_app_config.json",
    "paipai_config": _PROJECT_ROOT / "paipai_config.json",
    "rule_config": _PROJECT_ROOT / "pricing_rules.json",
    "app_config": _PROJECT_ROOT / "app_config.json",
    "price_history": _PROJECT_ROOT / "price_history.db",
    "same_sale": _DATA_DIR / "same_sale_groups.json",
    "browser_instances": _PROJECT_ROOT / "browser_instances.json",
}

CONFIG_FILE = str(paths.accounts_file)
ACCOUNTS_FILE = CONFIG_FILE
CACHE_FILE = str(paths.sold_cache_file)
ERP_CONFIG_FILE = str(paths.erp_config_file)
ERP_COST_MAP_FILE = str(paths.erp_cost_map_file)
WXAPP_CONFIG_FILE = str(paths.wxapp_config_file)
PAIPAI_CONFIG_FILE = str(paths.paipai_config_file)
RULE_CONFIG_FILE = str(paths.rule_config_file)
APP_CONFIG_FILE = str(paths.app_config_file)
PRICE_HISTORY_DB = str(paths.price_history_db)
SAME_SALE_FILE = str(paths.same_sale_file)
BROWSER_INSTANCES_FILE = str(paths.browser_instances_file)

ENGINE_WINDOW = 60
ENGINE_NEAR = 30
ENGINE_W_RECENT = 3
ENGINE_W_OLD = 1
ENGINE_MIN_SAMPLE = 5
FAST_SALE_HOURS = 24

PLATFORM_FEE_RATE = 0.05
STATION_SERVICE_FEE = 40
DEFAULT_PLATFORM_FEE_RATE = PLATFORM_FEE_RATE
DEFAULT_STATION_SERVICE_FEE = STATION_SERVICE_FEE

STALE_STAGE1_DAYS = 7
STALE_STAGE2_DAYS = 14
STALE_STAGE2_DROP_PCT = 5
STALE_STAGE2_DROP_MIN = 50
AUTO_REPRICE_INTERVAL = 120
AUTO_LIST_INTERVAL = 120
SALES_REPORT_INTERVAL = 60

AUTO_REPRICE_CUSTOM_OFFSET_MODE = "off"
AUTO_REPRICE_CUSTOM_OFFSET_VALUE = 0.0

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
WXAPP_SERVER_PORT = 8088


def ensure_parent_dir(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def resolve_storage_path(current_path: str | Path, legacy_path: str | Path | None = None) -> Path:
    current = Path(current_path)
    legacy = Path(legacy_path) if legacy_path else None
    if current.exists():
        return current
    if legacy and legacy.exists():
        return legacy
    return current


def atomic_write_text(path: str | Path, content: str, encoding: str = "utf-8") -> Path:
    target = ensure_parent_dir(path)
    with NamedTemporaryFile("w", encoding=encoding, delete=False, dir=str(target.parent)) as tmp:
        tmp.write(content)
        temp_name = tmp.name
    os.replace(temp_name, target)
    return target


def atomic_write_json(path: str | Path, data: Any) -> Path:
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_json_with_legacy(
    current_path: str | Path,
    legacy_path: str | Path | None = None,
    *,
    default_factory: Callable[[], Any] = dict,
):
    source = resolve_storage_path(current_path, legacy_path)
    if not source.exists():
        return default_factory(), Path(current_path)
    try:
        with open(source, "r", encoding="utf-8") as f:
            return json.load(f), source
    except Exception:
        return default_factory(), source


def save_json_with_legacy(data: Any, current_path: str | Path, legacy_path: str | Path | None = None) -> Path:
    target = Path(current_path)
    atomic_write_json(target, data)
    legacy = Path(legacy_path) if legacy_path else None
    if legacy and legacy.exists() and legacy.resolve() != target.resolve():
        try:
            legacy.unlink()
        except Exception:
            pass
    return target


class JsonConfigFile:
    def __init__(self, current_path: str | Path, legacy_path: str | Path | None = None):
        self.current_path = Path(current_path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.path = str(resolve_storage_path(self.current_path, self.legacy_path))

    def load(self, default_factory: Callable[[], Any] = dict):
        data, source = load_json_with_legacy(self.current_path, self.legacy_path, default_factory=default_factory)
        self.path = str(source)
        return data

    def save(self, data: Any):
        target = save_json_with_legacy(data, self.current_path, self.legacy_path)
        self.path = str(target)
        return target


class AppConfig:
    """运行时可修改的配置单例，持久化到 config/app_config.json"""
    _instance = None
    _data: dict = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._file = JsonConfigFile(APP_CONFIG_FILE, LEGACY_PATHS["app_config"])
            cls._instance._load()
        return cls._instance

    def _load(self):
        self._data = self._file.load(dict)

    def save(self):
        self._file.save(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()

    @property
    def platform_fee_rate(self): return self._data.get("platform_fee_rate", PLATFORM_FEE_RATE)
    @property
    def station_service_fee(self): return self._data.get("station_service_fee", STATION_SERVICE_FEE)
    @property
    def fast_sale_hours(self): return self._data.get("fast_sale_hours", FAST_SALE_HOURS)
    @property
    def engine_min_sample(self): return self._data.get("engine_min_sample", ENGINE_MIN_SAMPLE)
    @property
    def stale_stage1_days(self): return self._data.get("stale_stage1_days", STALE_STAGE1_DAYS)
    @property
    def stale_stage2_days(self): return self._data.get("stale_stage2_days", STALE_STAGE2_DAYS)
    @property
    def stale_stage2_drop_pct(self): return self._data.get("stale_stage2_drop_pct", STALE_STAGE2_DROP_PCT)
    @property
    def auto_reprice_interval(self): return self._data.get("auto_reprice_interval", AUTO_REPRICE_INTERVAL)
    @property
    def auto_list_interval(self): return self._data.get("auto_list_interval", AUTO_LIST_INTERVAL)
    @property
    def sales_report_interval(self): return self._data.get("sales_report_interval", SALES_REPORT_INTERVAL)
    @property
    def auto_reprice_custom_offset_mode(self): return self._data.get("auto_reprice_custom_offset_mode", AUTO_REPRICE_CUSTOM_OFFSET_MODE)
    @property
    def auto_reprice_custom_offset_value(self): return self._data.get("auto_reprice_custom_offset_value", AUTO_REPRICE_CUSTOM_OFFSET_VALUE)

    @property
    def manual_review_policy_enabled(self): return self._data.get("manual_review_policy_enabled", False)

    @property
    def report_wechat_enabled(self): return self._data.get("report_wechat_enabled", False)
    @property
    def probe_enabled(self): return self._data.get("probe_enabled", False)
    @property
    def probe_interval_minutes(self): return self._data.get("probe_interval_minutes", 240)
    @property
    def probe_delta(self): return self._data.get("probe_delta", 20)
    @property
    def probe_daily_max(self): return self._data.get("probe_daily_max", 6)
    @property
    def probe_guard_window_hours(self): return self._data.get("probe_guard_window_hours", 24)
    @property
    def probe_guard_cum_drop_limit(self): return self._data.get("probe_guard_cum_drop_limit", 120)
    @property
    def probe_guard_big_drop_threshold(self): return self._data.get("probe_guard_big_drop_threshold", 50)
    @property
    def probe_guard_big_drop_count(self): return self._data.get("probe_guard_big_drop_count", 2)
    @property
    def probe_fast_turnover_days(self): return self._data.get("probe_fast_turnover_days", 7)
    @property
    def probe_fast_delta(self): return self._data.get("probe_fast_delta", 15)
    @property
    def probe_fast_interval_minutes(self): return self._data.get("probe_fast_interval_minutes", 180)
    @property
    def probe_fast_daily_max(self): return self._data.get("probe_fast_daily_max", 8)
    @property
    def probe_slow_delta(self): return self._data.get("probe_slow_delta", 25)
    @property
    def probe_slow_interval_minutes(self): return self._data.get("probe_slow_interval_minutes", 360)
    @property
    def probe_slow_daily_max(self): return self._data.get("probe_slow_daily_max", 4)


cfg = AppConfig()
