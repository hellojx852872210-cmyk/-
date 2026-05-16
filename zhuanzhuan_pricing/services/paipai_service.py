# -*- coding: utf-8 -*-
"""
拍拍平台服务骨架
- 配置读写
- Session / Header 组装
- 商品列表请求骨架
- 基础健康检查
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import requests

from ..config import JsonConfigFile, LEGACY_PATHS, PAIPAI_CONFIG_FILE, REQUEST_TIMEOUT
from ..core.models import ProductDetail, ProductStatus
from ..core.utils import clean_model_name, extract_capacity_text, extract_color_text

PAIPAI_BASE_URL = "https://pp-api.jd.com"
PAIPAI_LIST_API_NAME = "pop.shop.product.item.sku.list"
PAIPAI_APP_KEY = "paipai-pop-shop"
PAIPAI_REFERER = "https://paipai.shop.jd.com/"
PAIPAI_ORIGIN = "https://paipai.shop.jd.com"
PAIPAI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


@dataclass
class PaipaiListResult:
    ok: bool
    message: str = ""
    items: list[ProductDetail] | None = None
    raw: dict | list | None = None


class PaipaiConfig:
    """拍拍平台配置读写"""

    def __init__(self, path: str = PAIPAI_CONFIG_FILE):
        legacy_path = None if path != PAIPAI_CONFIG_FILE else LEGACY_PATHS["paipai_config"]
        self._file = JsonConfigFile(path, legacy_path)
        self.path = self._file.path
        self._data: dict = {}
        self.load()

    def load(self):
        self._data = self._file.load(dict)
        self.path = self._file.path
        return dict(self._data)

    def save(self):
        target = self._file.save(self._data)
        self.path = str(target)
        return target

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()

    @property
    def cookie(self) -> str:
        return str(self._data.get("cookie", "") or "").strip()

    @property
    def app_key(self) -> str:
        return str(self._data.get("app_key", PAIPAI_APP_KEY) or PAIPAI_APP_KEY).strip()

    @property
    def api_name(self) -> str:
        return str(self._data.get("api_name", PAIPAI_LIST_API_NAME) or PAIPAI_LIST_API_NAME).strip()

    @property
    def login_type(self) -> str:
        return str(self._data.get("login_type", "pc") or "pc").strip()

    @property
    def body_type(self) -> str:
        return str(self._data.get("body_type", "json") or "json").strip()

    @property
    def sign(self) -> str:
        return str(self._data.get("sign", "") or "").strip()

    @property
    def body(self) -> str:
        value = self._data.get("body", "")
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value or "")

    @property
    def page_size(self) -> int:
        try:
            return max(1, int(self._data.get("page_size", 20) or 20))
        except Exception:
            return 20

    @property
    def origin(self) -> str:
        return str(self._data.get("origin", PAIPAI_ORIGIN) or PAIPAI_ORIGIN).strip()

    @property
    def referer(self) -> str:
        return str(self._data.get("referer", PAIPAI_REFERER) or PAIPAI_REFERER).strip()

    @property
    def user_agent(self) -> str:
        return str(self._data.get("user_agent", PAIPAI_USER_AGENT) or PAIPAI_USER_AGENT).strip()


class PaipaiClient:
    """拍拍平台请求客户端（当前先实现列表与连通性检查骨架）"""

    def __init__(self, config: PaipaiConfig):
        self.config = config
        self._lock = threading.Lock()
        self._session = self._make_session()

    def _make_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": self.config.origin,
            "Referer": self.config.referer,
            "User-Agent": self.config.user_agent,
            "Cookie": self.config.cookie,
        })
        return session

    def refresh_config(self):
        self.config.load()
        with self._lock:
            self._session = self._make_session()

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _request_params(self, *, timestamp: Optional[str] = None) -> dict[str, str]:
        ts = str(timestamp or self._timestamp())
        params = {
            "apiName": self.config.api_name,
            "appKey": self.config.app_key,
            "timestamp": ts,
            "loginType": self.config.login_type,
            "bodyType": self.config.body_type,
        }
        if self.config.sign:
            params["sign"] = self.config.sign
        return params

    def _request_body(self, *, page: int = 1, page_size: Optional[int] = None) -> dict[str, str]:
        body = self.config.body.strip()
        payload = {
            "pageNo": str(max(1, int(page))),
            "pageSize": str(max(1, int(page_size or self.config.page_size))),
        }
        if body:
            payload["body"] = body
        return payload

    def build_list_request(self, *, page: int = 1, page_size: Optional[int] = None, timestamp: Optional[str] = None) -> dict:
        params = self._request_params(timestamp=timestamp)
        data = self._request_body(page=page, page_size=page_size)
        url = f"{PAIPAI_BASE_URL}/api?{urlencode(params)}"
        return {
            "method": "POST",
            "url": url,
            "params": params,
            "data": data,
            "headers": dict(self._session.headers),
        }

    def health_check(self) -> tuple[bool, str]:
        if not self.config.cookie:
            return False, "未配置拍拍 Cookie"
        if not self.config.sign:
            return False, "未配置拍拍 sign"
        if not self.config.body:
            return False, "未配置拍拍列表 body"
        return True, "配置已具备基础请求条件"

    def fetch_items(self, *, page: int = 1, page_size: Optional[int] = None) -> PaipaiListResult:
        ok, message = self.health_check()
        if not ok:
            return PaipaiListResult(ok=False, message=message, items=[])

        request_spec = self.build_list_request(page=page, page_size=page_size)
        try:
            with self._lock:
                resp = self._session.post(
                    request_spec["url"],
                    data=request_spec["data"],
                    timeout=max(REQUEST_TIMEOUT, 15),
                )
            try:
                raw = resp.json()
            except Exception:
                text = resp.text[:500]
                return PaipaiListResult(ok=False, message=f"响应不是 JSON: HTTP {resp.status_code} {text}", items=[], raw=text)
            items = self._extract_items(raw)
            parsed = [self._parse_item(item) for item in items if isinstance(item, dict)]
            return PaipaiListResult(ok=True, message=f"成功返回 {len(parsed)} 条商品", items=parsed, raw=raw)
        except Exception as e:
            return PaipaiListResult(ok=False, message=f"请求失败: {e}", items=[])

    def fetch_all_on_sale(self, max_pages: int = 1) -> list[ProductDetail]:
        items: list[ProductDetail] = []
        for page in range(1, max(1, int(max_pages)) + 1):
            result = self.fetch_items(page=page)
            if not result.ok:
                break
            batch = result.items or []
            items.extend(batch)
            if not batch:
                break
        return items

    def _extract_items(self, raw) -> list[dict]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if not isinstance(raw, dict):
            return []

        candidates = [
            raw.get("data"),
            raw.get("result"),
            raw.get("body"),
            raw.get("data", {}).get("data") if isinstance(raw.get("data"), dict) else None,
            raw.get("data", {}).get("list") if isinstance(raw.get("data"), dict) else None,
            raw.get("result", {}).get("list") if isinstance(raw.get("result"), dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
            if isinstance(candidate, dict):
                for key in ("list", "rows", "items", "records"):
                    value = candidate.get(key)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
        return []

    def _parse_price(self, value) -> float:
        if value in (None, ""):
            return 0.0
        try:
            return float(value)
        except Exception:
            return 0.0

    def _first_text(self, raw: dict, *keys: str) -> str:
        for key in keys:
            value = raw.get(key)
            if value in (None, ""):
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def _parse_status(self, raw: dict):
        status_value = self._first_text(raw, "status", "statusCode", "itemStatus", "saleStatus")
        status = ProductStatus.from_raw(status_value)
        if status == ProductStatus.UNKNOWN:
            status = ProductStatus.ON_SALE
        status_text = self._first_text(raw, "statusName", "statusText", "statusDesc") or status.label
        return status, status_text

    def _parse_item(self, raw: dict) -> ProductDetail:
        title = self._first_text(raw, "skuTitle", "title", "name", "productName")
        model = self._first_text(raw, "model", "modelName") or clean_model_name(title)
        capacity = self._first_text(raw, "capacity", "memory") or extract_capacity_text(title)
        color = self._first_text(raw, "color", "colorName") or extract_color_text(title)
        status, status_text = self._parse_status(raw)
        return ProductDetail(
            product_id=self._first_text(raw, "skuId", "id", "productId"),
            qc_code=self._first_text(raw, "spuId", "qcCode", "checkCode"),
            title=title,
            current_price=self._parse_price(raw.get("price") or raw.get("salePrice") or raw.get("jdPrice")),
            status=status,
            status_text=status_text,
            imei=self._first_text(raw, "imei", "imei1", "sn"),
            model=model,
            condition=self._first_text(raw, "condition", "conditionName"),
            capacity=capacity,
            color=color,
            account_name="拍拍",
            cost_price=self._parse_price(raw.get("costPrice")),
            settle_price=self._parse_price(raw.get("settlePrice")),
        )
