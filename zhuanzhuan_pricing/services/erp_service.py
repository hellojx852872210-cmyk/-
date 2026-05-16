# -*- coding: utf-8 -*-
"""
爱管机 ERP 服务封装（旧版 Authorization + Version）
- 固定爱管机接口地址
- 商品列表拉取（在售/在库/已售）
- 成本价同步
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import requests

from ..config import ERP_CONFIG_FILE, LEGACY_PATHS, JsonConfigFile, REQUEST_TIMEOUT

ERP_BASE_URL = "https://api.aiguanji.com"
ERP_PUT_SHELF_URL = f"{ERP_BASE_URL}/api/v1/sale/put_shelf/index"
ERP_STOCK_URL = f"{ERP_BASE_URL}/api/v1/storage/product/index"
ERP_SOLD_URL = f"{ERP_BASE_URL}/api/v1/sale/order/index"
ERP_COST_TAX_RATE = 0.02
ERP_DEFAULT_PAGE_SIZE = 1000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class ErpItem:
    product_id: str
    qc_code: str
    title: str
    cost_price: float = 0.0
    status: str = ""
    listed_time: Optional[datetime.datetime] = None
    imei: str = ""


class ErpConfig:
    """爱管机 ERP 配置读写（兼容当前新框架调用方式）"""

    def __init__(self, path: str = ERP_CONFIG_FILE):
        legacy_path = None if path != ERP_CONFIG_FILE else LEGACY_PATHS["erp_config"]
        self._file = JsonConfigFile(path, legacy_path)
        self.path = self._file.path
        self._data: dict = {}
        self.load()

    def load(self):
        self._data = self._file.load(dict)
        self.path = self._file.path
        return dict(self._data)

    def save(self, token: Optional[str] = None, version: Optional[str] = None):
        if token is not None:
            self._data["token"] = str(token or "").strip()
        if version is not None:
            self._data["version"] = str(version or "").strip()
        target = self._file.save(self._data)
        self.path = str(target)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()

    @property
    def token(self) -> str:
        return str(self._data.get("token", self._data.get("access_token", "")) or "").strip()

    @property
    def version(self) -> str:
        return str(self._data.get("version", "") or "").strip()

    # 兼容旧调用字段，避免别处直接访问时报错
    @property
    def base_url(self) -> str:
        return ERP_BASE_URL

    @property
    def app_id(self) -> str:
        return ""

    @property
    def app_secret(self) -> str:
        return ""

    @property
    def access_token(self) -> str:
        return self.token

    @property
    def token_expires_at(self) -> Optional[datetime.datetime]:
        return None

    def save_token(self, token: str, expires_in: int = 0):
        self.save(token=token)


class ErpFetcher:
    """爱管机 ERP 数据拉取（旧版 Authorization + Version）"""

    def __init__(self, config: ErpConfig):
        self.config = config
        self._lock = threading.Lock()
        self._session = requests.Session()

    @staticmethod
    def _default_version() -> str:
        return datetime.datetime.now().strftime("%Y%m%d") + "01"

    @classmethod
    def refresh_version(cls) -> str:
        return cls._default_version()

    def check_and_refresh_token(self) -> bool:
        if not self.config.token:
            raise RuntimeError("ERP 配置缺少 Authorization Token，请先填写并保存")
        return True

    def _auth_headers(self) -> dict:
        self.check_and_refresh_token()
        return {
            "Authorization": self.config.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
            "Origin": "https://saas.aiguanji.com",
            "Referer": "https://saas.aiguanji.com/",
            "Version": self.config.version or self._default_version(),
        }

    def _fetch_all(
        self,
        url: str,
        payload: dict,
        on_log=None,
        on_page_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[dict]:
        all_items: list[dict] = []
        page = 1
        retried_version = False

        while True:
            request_payload = dict(payload)
            request_payload["page"] = page
            body = None
            request_error = None
            timeout_seconds = max(REQUEST_TIMEOUT, 30)
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    with self._lock:
                        resp = self._session.post(
                            url,
                            json=request_payload,
                            headers=self._auth_headers(),
                            timeout=timeout_seconds,
                        )
                    try:
                        body = resp.json()
                    except Exception:
                        resp.raise_for_status()
                        raise RuntimeError(f"第{page}页响应无法解析")
                    request_error = None
                    break
                except RuntimeError:
                    raise
                except Exception as e:
                    request_error = e
                    if attempt >= max_attempts:
                        break
                    if on_log:
                        on_log(
                            f"  ⚠️ 第{page}页请求失败（第{attempt}次）：{e}；准备重试..."
                        )

            if request_error is not None or body is None:
                raise RuntimeError(
                    f"第{page}页请求失败（重试{max_attempts}次，超时{timeout_seconds}s）：{request_error}"
                ) from request_error

            code = body.get("code")
            msg = body.get("msg", "")
            if str(code) != "0" and "新版本" in str(msg) and not retried_version:
                self.config.set("version", self.refresh_version())
                if on_log:
                    on_log(f"  ⚡ 检测到版本更新，已刷新为 {self.config.version}，重试...")
                retried_version = True
                continue

            if str(code) != "0":
                raise RuntimeError(f"ERP 返回错误 code={code!r}：{msg or str(body)[:200]}")

            outer = body.get("data", {})
            if isinstance(outer, dict):
                inner = outer.get("data") or outer.get("list") or []
                last_page = outer.get("last_page") or outer.get("lastPage") or 1
                per_page = outer.get("per_page") or outer.get("pageSize") or payload.get("limit", ERP_DEFAULT_PAGE_SIZE)
            elif isinstance(outer, list):
                inner = outer
                last_page = 1
                per_page = payload.get("limit", ERP_DEFAULT_PAGE_SIZE)
            else:
                inner = []
                last_page = 1
                per_page = payload.get("limit", ERP_DEFAULT_PAGE_SIZE)

            if on_page_progress:
                try:
                    on_page_progress(int(page), max(int(last_page or 1), 1))
                except Exception:
                    pass

            if not inner:
                break

            all_items.extend(inner)
            if on_log:
                on_log(f"  已拉取 {len(all_items)} 条…")

            if page >= int(last_page or 1) or len(inner) < int(per_page or ERP_DEFAULT_PAGE_SIZE):
                break
            page += 1
            retried_version = False

        return all_items

    @staticmethod
    def _parse_datetime(value) -> Optional[datetime.datetime]:
        if value in (None, "", 0):
            return None
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            try:
                return datetime.datetime.fromtimestamp(ts)
            except Exception:
                return None
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("T", " ").replace("Z", "")
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                return datetime.datetime.strptime(text, fmt)
            except Exception:
                continue
        try:
            return datetime.datetime.fromisoformat(text)
        except Exception:
            return None

    @staticmethod
    def _as_dict(value) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _first_non_empty(raw: dict, *keys: str) -> str:
        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                text = str(value).strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _parse_cost(raw: dict) -> float:
        value = raw.get("real_cost_price")
        if value in (None, "", 0):
            value = raw.get("cost_price")
        if value in (None, "", 0):
            value = raw.get("cost")
        if isinstance(value, str):
            value = value.replace(",", "").replace("¥", "").strip()
        try:
            cost = float(value or 0.0)
        except Exception:
            return 0.0
        if cost <= 0:
            return 0.0
        return round(cost, 2)


    @staticmethod
    def _normalize_code(value) -> str:
        return str(value or "").strip()

    @classmethod
    def _collect_code_candidates(cls, *values) -> list[str]:
        codes: list[str] = []
        seen: set[str] = set()
        for value in values:
            if isinstance(value, (list, tuple, set)):
                nested_values = value
            else:
                nested_values = (value,)
            for item in nested_values:
                code = cls._normalize_code(item)
                if not code or code in seen:
                    continue
                seen.add(code)
                codes.append(code)
        return codes

    @classmethod
    def _extract_qc_code(cls, raw: dict, attrs: dict) -> str:
        return next(iter(cls._collect_code_candidates(
            raw.get("sale_no"),
            raw.get("saleNo"),
            raw.get("qc_code"),
            raw.get("qcCode"),
            raw.get("check_code"),
            raw.get("checkCode"),
            raw.get("inspection_code"),
            raw.get("inspectionCode"),
            raw.get("machine_code"),
            raw.get("machineCode"),
            raw.get("goods_sn"),
            raw.get("goodsSn"),
            raw.get("serial_no"),
            raw.get("serialNo"),
            raw.get("biz_no"),
            raw.get("bizNo"),
            attrs.get("质检码"),
            attrs.get("质检编号"),
            attrs.get("机器码"),
            attrs.get("编码"),
            attrs.get("商品编码"),
            attrs.get("编号"),
        )), "")

    @classmethod
    def _extract_imei(cls, raw: dict, attrs: dict) -> str:
        return next(iter(cls._collect_code_candidates(
            raw.get("imei"),
            raw.get("imei1"),
            raw.get("imei_1"),
            raw.get("imei2"),
            raw.get("imei_2"),
            raw.get("sn"),
            raw.get("serial_no"),
            raw.get("serialNo"),
            raw.get("serial_number"),
            raw.get("serialNumber"),
            raw.get("goods_sn"),
            raw.get("goodsSn"),
            attrs.get("IMEI"),
            attrs.get("IMEI1"),
            attrs.get("IMEI2"),
            attrs.get("串号"),
            attrs.get("序列号"),
            attrs.get("SN"),
        )), "")

    @staticmethod
    def _fallback_status(status: str) -> str:
        return {
            "on_sale": "0",
            "in_stock": "60",
            "sold": "80",
        }.get(status, "")

    def _parse_item(self, raw: dict, status: str) -> ErpItem:
        attrs = self._as_dict(raw.get("selected_attr"))
        listed_time = (
            self._parse_datetime(raw.get("put_shelf_time"))
            or self._parse_datetime(raw.get("listed_time"))
            or self._parse_datetime(raw.get("list_time"))
            or self._parse_datetime(raw.get("created_at"))
            or self._parse_datetime(raw.get("updated_at"))
            or self._parse_datetime(raw.get("sale_time"))
            or self._parse_datetime(raw.get("sold_time"))
        )
        title = self._first_non_empty(raw, "name", "title", "product_name", "goods_name")
        if not title:
            title = self._first_non_empty(attrs, "型号", "机型")
        return ErpItem(
            product_id=self._first_non_empty(raw, "product_id", "productId", "id"),
            qc_code=self._extract_qc_code(raw, attrs),
            imei=self._extract_imei(raw, attrs),
            title=title,
            cost_price=self._parse_cost(raw),
            status=self._first_non_empty(raw, "status", "sale_status", "product_status") or self._fallback_status(status),
            listed_time=listed_time,
        )


    def fetch_put_shelf(
        self,
        on_log=None,
        on_page_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[dict]:
        payload = {
            "page": 1,
            "limit": ERP_DEFAULT_PAGE_SIZE,
            "value1": "",
            "cate_id": "",
            "brand_id": "",
            "product_id": "",
            "sale_channel_id": "",
            "selected_attr": {},
        }
        return self._fetch_all(ERP_PUT_SHELF_URL, payload, on_log, on_page_progress=on_page_progress)

    def fetch_stock(
        self,
        on_log=None,
        on_page_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[dict]:
        payload = {
            "page": 1,
            "limit": ERP_DEFAULT_PAGE_SIZE,
            "value1": "",
            "cate_id": "",
            "brand_id": "",
            "product_id": "",
            "warehouse_id": "",
            "selected_attr": {},
        }
        try:
            return self._fetch_all(ERP_STOCK_URL, payload, on_log, on_page_progress=on_page_progress)
        except Exception as e:
            if on_log:
                on_log(f"  ⚠️ 在库接口暂不可用（{e}），跳过")
            return []

    def fetch_sold(
        self,
        on_log=None,
        on_page_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[dict]:
        payload = {
            "page": 1,
            "limit": ERP_DEFAULT_PAGE_SIZE,
            "value1": "",
            "cate_id": "",
            "brand_id": "",
            "product_id": "",
            "sale_channel_id": "",
            "selected_attr": {},
        }
        try:
            return self._fetch_all(ERP_SOLD_URL, payload, on_log, on_page_progress=on_page_progress)
        except Exception as e:
            if on_log:
                on_log(f"  ⚠️ 已售接口暂不可用（{e}），跳过")
            return []

    def fetch_on_sale_items(
        self,
        on_log=None,
        on_page_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[ErpItem]:
        return [self._parse_item(raw, "on_sale") for raw in self.fetch_put_shelf(on_log=on_log, on_page_progress=on_page_progress)]

    def fetch_in_stock_items(
        self,
        on_log=None,
        on_page_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[ErpItem]:
        return [self._parse_item(raw, "in_stock") for raw in self.fetch_stock(on_log=on_log, on_page_progress=on_page_progress)]

    def fetch_sold_items(
        self,
        since: Optional[datetime.datetime] = None,
        on_log=None,
        on_page_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[ErpItem]:
        items = [self._parse_item(raw, "sold") for raw in self.fetch_sold(on_log=on_log, on_page_progress=on_page_progress)]
        if since is None:
            return items
        filtered: list[ErpItem] = []
        for item in items:
            if item.listed_time is None or item.listed_time >= since:
                filtered.append(item)
        return filtered

    def build_cost_map(self, items: List[ErpItem]) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for item in items:
            if not item.product_id or item.cost_price <= 0:
                continue
            result[str(item.product_id)] = float(item.cost_price)
        return result
