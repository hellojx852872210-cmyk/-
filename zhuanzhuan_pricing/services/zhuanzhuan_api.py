# -*- coding: utf-8 -*-
"""
转转 API 封装
ImeiService  — 查询商品、改价、定价上架
DataFetcher  — 拉取历史成交数据
每个账号共享一个 requests.Session（连接池复用）
"""
from __future__ import annotations

import datetime
import re
import threading
import time
from typing import Callable, List, Optional, Tuple

import requests

from ..config import MAX_RETRIES, REQUEST_TIMEOUT
from ..core.models import ProductDetail, ProductStatus, SoldRecord
from ..core.utils import build_condition, clean_model_name, round_to_8

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DEFAULT_PAGE_SIZE = 50
MAX_FETCH_PAGES = 200
MERCHANT_LIST_CONCURRENCY = 2
MERCHANT_LIST_RATE_LIMIT_RETRIES = 3

MERCHANT_LIST_SEMAPHORE = threading.BoundedSemaphore(MERCHANT_LIST_CONCURRENCY)

MERCHANT_LIST_URL = "https://b.zhuanzhuan.com/gatewayapi/scm_render_product/merchantProductList"
PRICE_POPUP_URL = "https://b.zhuanzhuan.com/gatewayapi/scm_render/newConfirmPricePopup"
PRICE_CHECK_URL = "https://b.zhuanzhuan.com/api/scm_render/merchantPriceCheck"
PRICE_CHANGE_URL = "https://b.zhuanzhuan.com/api/scm_render/merchantChangePrice"
PRICE_CONFIRM_URL = "https://b.zhuanzhuan.com/gatewayapi/scm_render/merchantConfirmPrice"
ESTIMATE_PRICE_URL = "https://b.zhuanzhuan.com/api/scm_render/merchantQueryEstimatePriceInfo"
DOUBLE_GRADE_PURCHASE_PRICE_URL = "https://b.zhuanzhuan.com/gatewayapi/scm_render/queryDoubleGradePurchasePrice"
POST_QC_DIFF_URL = "https://b.zhuanzhuan.com/api/supply_product/merchantQueryPostQcDiff"
WITHDRAW_URL = "https://b.zhuanzhuan.com/gatewayapi/supply_product/merchantWithdraw"
LOOKUP_BATCH_SIZE = 50

LOOKUP_STATUS_ORDER = ("70", "0", "60", "80", "1")
IMEI_CODE_RE = re.compile(r"\d{15}")


def _chunked(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [values]
    return [values[i:i + size] for i in range(0, len(values), size)]


def _normalize_lookup_code(code: str) -> str:
    return str(code or "").strip()


def _is_imei_code(code: str) -> bool:
    normalized = _normalize_lookup_code(code)
    return bool(normalized) and IMEI_CODE_RE.fullmatch(normalized) is not None


def _group_lookup_codes(codes: list[str]) -> tuple[list[str], list[str]]:
    imei_codes: list[str] = []
    qc_codes: list[str] = []
    for code in codes or []:
        if _is_imei_code(code):
            imei_codes.append(code)
        else:
            qc_codes.append(code)
    return imei_codes, qc_codes


def _normalize_tail8_price(price: float) -> float:
    rounded = int(round(float(price), 0))
    if rounded <= 0:
        return 0.0
    return float(round_to_8(rounded))


def _make_session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Cookie": cookie,
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": USER_AGENT,
    })
    return s


def _post_json_with_retry(
    session: requests.Session,
    url: str,
    payload: dict,
    max_retries: int = MAX_RETRIES,
    timeout: int = REQUEST_TIMEOUT,
    raise_for_status: bool = True,
) -> tuple[requests.Response, dict]:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = session.post(url, json=payload, timeout=timeout)
            if raise_for_status:
                resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                body = {}
            return resp, body
        except requests.RequestException as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1.5 ** attempt)
                continue
            raise RuntimeError(f"请求失败（{max_retries}次）: {e}") from e
    raise RuntimeError(f"请求失败: {last_error}")


def _extract_total(data: dict) -> int:
    for key in ("total", "totalCount", "count", "pageTotal"):
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _merchant_data(resp: dict) -> dict:
    code = resp.get("code")
    if code is None:
        code = resp.get("respCode")
    msg = resp.get("msg") or resp.get("respMsg") or resp.get("errMsg") or "未知错误"
    if code is not None and str(code) not in ("0", "200"):
        raise RuntimeError(f"商家接口返回错误: {msg}")
    return resp.get("data") or resp.get("respData") or {}


def _is_rate_limit_message(message: str) -> bool:
    text = str(message or "").strip().lower()
    return any(keyword in text for keyword in ("操作过于频繁", "too frequent", "rate limit", "频繁", "稍后重试"))


def _normalize_status_code(raw) -> str:
    if raw in (None, "") or isinstance(raw, (dict, list, tuple, set)):
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if text in {"0", "1", "60", "80", "99", "-1"}:
        return text
    mapping = {
        "onsale": "0",
        "on_sale": "0",
        "sale": "0",
        "selling": "0",
        "published": "0",
        "offsale": "1",
        "off_sale": "1",
        "off": "1",
        "removed": "1",
        "unpublished": "1",
        "returned": "1",
        "return": "1",
        "returned_to_seller": "1",
        "returned_to_merchant": "1",
        "back": "1",
        "instock": "60",
        "in_stock": "60",
        "stock": "60",
        "draft": "60",
        "pending": "60",
        "sold": "80",
        "deal": "80",
        "dealed": "80",
        "done": "80",
        "qc": "99",
        "checking": "99",
        "qualitycheck": "99",
        "已退回": "1",
        "退回": "1",
        "退回中": "1",
        "已退货": "1",
        "退货": "1",
        "已返还": "1",
        "返还": "1",
    }
    compact = text.replace(" ", "").replace("-", "_").lower()
    return mapping.get(compact, "")


def _merchant_items(resp: dict) -> list[dict]:
    return _merchant_data(resp).get("list", []) or []


def _qc_code_rank(raw_qc) -> int:
    text = str(raw_qc or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return -1
    try:
        return int(digits)
    except Exception:
        return -1


def _extract_estimate_settle_price(resp_data, product_id: str) -> Optional[float]:
    if isinstance(resp_data, dict):
        candidates = resp_data.get("list") or resp_data.get("items") or []
        if not candidates and resp_data:
            candidates = [resp_data]
    elif isinstance(resp_data, list):
        candidates = resp_data
    else:
        candidates = []

    target_product_id = str(product_id or "")
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item_product_id = str(item.get("productId") or "")
        if target_product_id and item_product_id and item_product_id != target_product_id:
            continue

        lowest_prices = item.get("lowestSettlePrice") or []
        parsed_prices: list[float] = []
        if isinstance(lowest_prices, list):
            for price_item in lowest_prices:
                raw_price = price_item.get("price") if isinstance(price_item, dict) else price_item
                if raw_price in (None, ""):
                    continue
                try:
                    parsed_prices.append(float(raw_price))
                except (TypeError, ValueError):
                    continue
        elif lowest_prices not in (None, ""):
            try:
                parsed_prices.append(float(lowest_prices))
            except (TypeError, ValueError):
                pass
        if parsed_prices:
            return min(parsed_prices) / 100

        settle_price = item.get("settlePrice")
        if settle_price not in (None, ""):
            try:
                return float(settle_price) / 100
            except (TypeError, ValueError):
                continue
    return None


def _build_product_params(properties) -> str:
    parts: list[str] = []
    for prop in properties or []:
        if not isinstance(prop, dict):
            continue
        pn_id = str(prop.get("pnId") or "").strip()
        pv_id = str(prop.get("pvId") or "").strip()
        if not pn_id or not pv_id or pv_id.lower() == "null":
            continue
        parts.append(f"{pn_id}:{pv_id}")
    return (";".join(parts) + ";") if parts else ""


def _parse_datetime_value(raw) -> Optional[datetime.datetime]:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime.datetime):
        return raw
    try:
        if isinstance(raw, (int, float)):
            ts = float(raw)
            if ts > 10_000_000_000:
                ts /= 1000
            return datetime.datetime.fromtimestamp(ts)
        s = str(raw).strip()
        if not s:
            return None
        if s.isdigit():
            ts = int(s)
            if ts > 10_000_000_000:
                ts /= 1000
            return datetime.datetime.fromtimestamp(ts)
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _status_label_to_raw(label: str) -> str:
    normalized = str(label or "").strip()
    normalized = normalized.replace("（", "(").replace("）", ")")
    normalized = normalized.replace("商品", "")
    compact = normalized.replace(" ", "")
    mapping = {
        "在售": "0",
        "已上架": "0",
        "上架中": "0",
        "销售中": "0",
        "出售中": "0",
        "已售": "80",
        "已售出": "80",
        "成交": "80",
        "已成交": "80",
        "下架": "1",
        "已下架": "1",
        "已停售": "1",
        "已退回": "1",
        "退回": "1",
        "退回中": "1",
        "已退货": "1",
        "退货": "1",
        "已返还": "1",
        "返还": "1",
        "未上架": "60",
        "待上架": "60",
        "待发布": "60",
        "未发布": "60",
        "质检中": "99",
        "待质检": "99",
        "质检": "99",
        "检测中": "99",
    }
    if compact in mapping:
        return mapping[compact]
    for key, value in mapping.items():
        if key and key in compact:
            return value
    return "-1"


def _extract_status(item: dict, fallback_status: str = "") -> tuple[str, str]:
    state = item.get("state") or {}

    text_candidates = (
        state.get("statusName"),
        state.get("statusDesc"),
        state.get("statusText"),
        state.get("statusLabel"),
        state.get("desc"),
        state.get("text"),
        item.get("statusDesc"),
        item.get("statusName"),
        item.get("productStatusDesc"),
        item.get("productStatusName"),
        item.get("statusText"),
        item.get("statusLabel"),
        item.get("statusDisplay"),
        item.get("statusStr"),
        item.get("saleStatusName"),
        item.get("bizStatusDesc"),
    )
    status_text = ""
    for text in text_candidates:
        text_value = str(text or "").strip()
        if text_value:
            status_text = text_value
            break

    raw_candidates = (
        state.get("status"),
        state.get("statusCode"),
        state.get("statusValue"),
        state.get("bizStatus"),
        state.get("saleStatus"),
        item.get("status"),
        item.get("productStatus"),
        item.get("statusCode"),
        item.get("productStatusCode"),
        item.get("saleStatus"),
        item.get("statusValue"),
        item.get("bizStatus"),
    )
    for raw in raw_candidates:
        normalized = _normalize_status_code(raw)
        if normalized:
            return normalized, status_text or str(raw).strip()

    if status_text:
        mapped = _status_label_to_raw(status_text)
        if mapped != "-1":
            return mapped, status_text

    normalized_fallback = _normalize_status_code(fallback_status)
    if normalized_fallback:
        fallback_label = getattr(ProductStatus.from_raw(normalized_fallback), "label", "")
        return normalized_fallback, status_text or fallback_label or str(fallback_status).strip()

    return "-1", status_text


class ImeiService:
    MERCHANT_LIST_URL = MERCHANT_LIST_URL
    PRICE_POPUP_URL = PRICE_POPUP_URL
    PRICE_CHECK_URL = PRICE_CHECK_URL
    PRICE_CHANGE_URL = PRICE_CHANGE_URL
    PRICE_CONFIRM_URL = PRICE_CONFIRM_URL
    ESTIMATE_PRICE_URL = ESTIMATE_PRICE_URL
    DOUBLE_GRADE_PURCHASE_PRICE_URL = DOUBLE_GRADE_PURCHASE_PRICE_URL
    POST_QC_DIFF_URL = POST_QC_DIFF_URL
    WITHDRAW_URL = WITHDRAW_URL

    def __init__(self, account_name: str, cookie: str):
        self.account_name = account_name
        self.cookie = cookie
        self._session = _make_session(cookie)
        self._lock = threading.Lock()

    def refresh_cookie(self, new_cookie: str):
        with self._lock:
            self.cookie = new_cookie
            self._session = _make_session(new_cookie)

    def check_cookie_valid(self) -> tuple[bool, str]:
        payload = {
            "query": {
                "pageNum": 1,
                "pageSize": 1,
                "statusList": ["0"],
                "tagIds": [],
                "noTagIds": [],
                "labels": [],
                "salesInShop": False,
            }
        }
        try:
            with self._lock:
                resp, body = _post_json_with_retry(
                    self._session,
                    self.MERCHANT_LIST_URL,
                    payload,
                    timeout=min(8, REQUEST_TIMEOUT),
                    raise_for_status=False,
                )
        except Exception as e:
            return False, str(e)

        if resp.status_code in (401, 403):
            msg = body.get("msg") or body.get("respMsg") or body.get("errMsg") or ""
            return False, msg or f"HTTP 状态码异常: {resp.status_code}"

        code = body.get("code")
        if code is None:
            code = body.get("respCode")
        if code is None:
            return False, "商家接口返回错误: 响应缺少状态码"

        try:
            _merchant_data(body)
        except Exception as exc:
            return False, str(exc)
        return True, ""

    def fetch_by_codes(
        self,
        codes: list[str],
        statuses: tuple[str, ...] = LOOKUP_STATUS_ORDER,
        batch_size: int = LOOKUP_BATCH_SIZE,
    ) -> tuple[list[ProductDetail], list[str]]:
        normalized_codes: list[str] = []
        seen_codes: set[str] = set()
        for code in codes or []:
            normalized = _normalize_lookup_code(code)
            if not normalized or normalized in seen_codes:
                continue
            seen_codes.add(normalized)
            normalized_codes.append(normalized)
        if not normalized_codes:
            return [], []

        status_order = tuple(statuses or LOOKUP_STATUS_ORDER) or LOOKUP_STATUS_ORDER
        found_by_code: dict[str, ProductDetail] = {}
        found_product_ids: set[str] = set()

        def collect_batch(batch: list[str]) -> None:
            imei_batch, qc_batch = _group_lookup_codes(batch)
            if not imei_batch and not qc_batch:
                return
            for status in status_order:
                query_base = {
                    "pageNum": 1,
                    "pageSize": max(DEFAULT_PAGE_SIZE, len(batch)),
                    "tagIds": [],
                    "noTagIds": [],
                    "labels": [],
                    "statusList": [status],
                    "salesInShop": False,
                }
                query_variants: list[dict] = []
                if imei_batch:
                    query_variants.append({**query_base, "imeis": imei_batch})
                if qc_batch:
                    query_variants.append({**query_base, "qcCodes": qc_batch})
                for query in query_variants:
                    body = self._merchant_product_list(query)
                    items = _merchant_items(body)
                    if not items:
                        continue
                    for item in items:
                        detail = self._parse_product_detail(item, fallback_status=status)
                        if detail is None:
                            continue
                        if detail.product_id and detail.product_id in found_product_ids:
                            continue
                        for code in self._detail_lookup_codes(detail):
                            if code in found_by_code:
                                continue
                            found_by_code[code] = detail
                        if detail.product_id:
                            found_product_ids.add(detail.product_id)

        effective_batch_size = batch_size or LOOKUP_BATCH_SIZE
        for batch in _chunked(normalized_codes, effective_batch_size):
            try:
                collect_batch(batch)
            except Exception:
                fallback_details = self._fallback_fetch_by_codes(batch, status_order)
                for detail in fallback_details:
                    if detail is None:
                        continue
                    if detail.product_id and detail.product_id in found_product_ids:
                        continue
                    for code in self._detail_lookup_codes(detail):
                        if code in found_by_code:
                            continue
                        found_by_code[code] = detail
                    if detail.product_id:
                        found_product_ids.add(detail.product_id)

        details: list[ProductDetail] = []
        missing: list[str] = []
        added_product_ids: set[str] = set()
        for code in normalized_codes:
            detail = found_by_code.get(code)
            if detail is None:
                missing.append(code)
                continue
            if detail.product_id and detail.product_id in added_product_ids:
                continue
            details.append(detail)
            if detail.product_id:
                added_product_ids.add(detail.product_id)
        return details, missing

    def _fallback_fetch_by_codes(
        self,
        codes: list[str],
        statuses: tuple[str, ...],
    ) -> list[ProductDetail]:
        details: list[ProductDetail] = []
        seen_product_ids: set[str] = set()
        for code in codes:
            field_name = "imeis" if _is_imei_code(code) else "qcCodes"
            secondary_field = "qcCodes" if field_name == "imeis" else "imeis"
            try:
                detail = self._query_by_field(field_name, code, statuses=statuses, fallback_code=code)
            except Exception:
                detail = None
            if detail is None:
                try:
                    detail = self._query_by_field(secondary_field, code, statuses=statuses, fallback_code=code)
                except Exception:
                    detail = None
            if detail is None:
                continue
            if detail.product_id and detail.product_id in seen_product_ids:
                continue
            details.append(detail)
            if detail.product_id:
                seen_product_ids.add(detail.product_id)
        return details

    def _detail_lookup_codes(self, detail: ProductDetail) -> set[str]:
        codes: set[str] = set()
        qc_code = _normalize_lookup_code(getattr(detail, "qc_code", ""))
        imei = _normalize_lookup_code(getattr(detail, "imei", ""))
        if qc_code:
            codes.add(qc_code)
        if imei:
            codes.add(imei)
        return codes

    def query_by_qc_code(self, qc_code: str) -> Optional[ProductDetail]:
        return self._lookup(qc_code, primary_field="qcCodes", secondary_field="imeis")

    def query_by_imei(self, imei: str) -> Optional[ProductDetail]:
        return self._lookup(imei, primary_field="imeis", secondary_field="qcCodes")

    def fetch_on_sale(self, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> Tuple[List[ProductDetail], int]:
        body = self._merchant_product_list({
            "pageNum": page,
            "pageSize": page_size,
            "tagIds": [],
            "noTagIds": [],
            "labels": [],
            "statusList": ["0"],
            "salesInShop": False,
        })
        data = _merchant_data(body)
        total = _extract_total(data)
        items = [self._parse_product_detail(item) for item in data.get("list", []) or []]
        return [item for item in items if item is not None], total

    def fetch_all_on_sale(self) -> List[ProductDetail]:
        all_items: list[ProductDetail] = []
        page = 1
        page_size = DEFAULT_PAGE_SIZE
        while page <= MAX_FETCH_PAGES:
            items, total = self.fetch_on_sale(page, page_size)
            all_items.extend(items)
            if not items:
                break
            if total and len(all_items) >= total:
                break
            if len(items) < page_size:
                break
            page += 1
        for item in all_items:
            item.account_name = self.account_name
        return all_items

    def fetch_settle_price(self, product_id: str) -> Optional[float]:
        detail = self._query_product_by_id(product_id, statuses=("0", "60", "80", "1"))
        return detail.settle_price if detail else None

    def estimate_settle_price(self, product_id: str, price: float) -> Optional[float]:
        if not product_id or price <= 0:
            return None
        payload = {
            "query": {
                "productPriceList": [
                    {
                        "productId": str(product_id),
                        "price": int(round(price * 100)),
                    }
                ]
            }
        }
        try:
            with self._lock:
                _resp, body = _post_json_with_retry(self._session, ESTIMATE_PRICE_URL, payload)
        except Exception:
            return None
        resp_data = body.get("respData") or body.get("data") or []
        return _extract_estimate_settle_price(resp_data, str(product_id))

    def change_price(self, product: ProductDetail | str, new_price: float) -> Tuple[bool, str]:
        try:
            detail = product if isinstance(product, ProductDetail) else None
            product_id = detail.product_id if detail is not None else str(product)
            if detail is None:
                detail = self._query_product_by_id(product_id, statuses=("0", "60", "80", "1"))
            if detail is None:
                return False, f"未找到商品 [{product_id}]"
            raw_status = str(getattr(detail.status, "value", detail.status) or "").strip()
            force_list = raw_status == ProductStatus.NOT_LISTED.value
            pre_price = 0.0 if force_list else detail.current_price
            return self._submit_price(product_id, new_price, pre_price=pre_price, force_list=force_list)
        except Exception as e:
            return False, str(e)

    def list_product(self, product_id: str, price: float, qc_code: str = "") -> Tuple[bool, str]:
        try:
            return self._submit_price(product_id, price, pre_price=0.0, force_list=True)
        except Exception as e:
            return False, str(e)

    def withdraw_product(self, product_id: str, withdraw_reason: int = 2) -> Tuple[bool, str]:
        product_id = str(product_id or "").strip()
        if not product_id:
            return False, "商品ID无效"
        payload = {
            "withdrawCommand": {
                "productIds": [product_id],
                "withdrawReason": int(withdraw_reason),
            }
        }
        try:
            with self._lock:
                _resp, body = _post_json_with_retry(self._session, self.WITHDRAW_URL, payload)
        except Exception as e:
            return False, str(e)

        resp_code = str(body.get("respCode") or body.get("code") or "")
        if resp_code not in ("0", "200"):
            return False, body.get("errMsg") or body.get("errorMsg") or body.get("respMsg") or body.get("msg") or "下架失败"

        resp_data = body.get("respData") or body.get("data") or {}
        success_ids = resp_data.get("successIds") or []
        if product_id not in {str(x) for x in success_ids}:
            fail_map = resp_data.get("failProductIdWithMsg") or []
            if fail_map:
                first = fail_map[0]
                if isinstance(first, dict):
                    return False, str(first.get("msg") or first.get("message") or first.get("errorMsg") or "下架失败")
            return False, body.get("errMsg") or body.get("errorMsg") or body.get("respMsg") or body.get("msg") or "下架失败"
        return True, "下架成功"

    def _lookup(self, code: str, primary_field: str, secondary_field: str) -> Optional[ProductDetail]:
        code = str(code or "").strip()
        if not code:
            return None

        statuses = ("0", "60", "80", "1")
        for field in (primary_field, secondary_field):
            detail = self._query_by_field(field, code, statuses=statuses, fallback_code=code)
            if detail is not None:
                return detail
        return None

    def _query_by_field(
        self,
        field_name: str,
        value: str,
        statuses: tuple[str, ...],
        fallback_code: str = "",
    ) -> Optional[ProductDetail]:
        matched_unknown: Optional[ProductDetail] = None
        for status in statuses:
            body = self._merchant_product_list({
                "pageNum": 1,
                "pageSize": 50,
                "tagIds": [],
                "noTagIds": [],
                "labels": [],
                field_name: [value],
                "statusList": [status],
                "salesInShop": False,
            })
            items = _merchant_items(body)
            if not items:
                continue
            best_item = max(items, key=lambda item: _qc_code_rank(item.get("qcCode")))
            detail = self._parse_product_detail(best_item, fallback_code=fallback_code, fallback_status=status)
            if detail is None:
                continue
            if detail.status == ProductStatus.UNKNOWN:
                matched_unknown = detail
                continue
            return detail
        return matched_unknown


    def _query_product_by_id(self, product_id: str, statuses: tuple[str, ...]) -> Optional[ProductDetail]:
        matched_unknown: Optional[ProductDetail] = None
        for status in statuses:
            body = self._merchant_product_list({
                "pageNum": 1,
                "pageSize": 20,
                "tagIds": [],
                "noTagIds": [],
                "labels": [],
                "productIds": [product_id],
                "statusList": [status],
                "salesInShop": False,
            })
            items = _merchant_items(body)
            for item in items:
                item_product_id = str(item.get("productId") or ((item.get("productIds") or [""])[0] or ""))
                if item_product_id == str(product_id):
                    detail = self._parse_product_detail(item, fallback_status=status)
                    if detail is None:
                        continue
                    if detail.status == ProductStatus.UNKNOWN:
                        matched_unknown = detail
                        continue
                    return detail
        return matched_unknown

    def _merchant_product_list(self, query: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(MERCHANT_LIST_RATE_LIMIT_RETRIES):
            try:
                with MERCHANT_LIST_SEMAPHORE:
                    with self._lock:
                        _resp, body = _post_json_with_retry(self._session, self.MERCHANT_LIST_URL, {"query": query})
                body = body or {}
                code = body.get("code")
                if code is None:
                    code = body.get("respCode")
                msg = body.get("msg") or body.get("respMsg") or body.get("errMsg") or ""
                if code is not None and str(code) not in ("0", "200") and _is_rate_limit_message(msg):
                    if attempt < MERCHANT_LIST_RATE_LIMIT_RETRIES - 1:
                        time.sleep(0.8 * (attempt + 1))
                        continue
                return body
            except Exception as e:
                last_error = e
                if _is_rate_limit_message(e):
                    if attempt < MERCHANT_LIST_RATE_LIMIT_RETRIES - 1:
                        time.sleep(0.8 * (attempt + 1))
                        continue
                raise RuntimeError(f"商家接口请求失败: {e}") from e
        raise RuntimeError(f"商家接口请求失败: {last_error}")

    def _submit_price(self, product_id: str, new_price: float, pre_price: float, force_list: bool) -> Tuple[bool, str]:
        normalized_new_price = _normalize_tail8_price(new_price)
        if normalized_new_price <= 0:
            return False, "价格无效"

        action_desc = "上架" if force_list else "改价"
        popup_payload = {
            "confirmPricePopupQuery": {
                "productIds": [product_id],
                "groupBySku": 1,
            }
        }
        with self._lock:
            _resp, popup_body = _post_json_with_retry(self._session, PRICE_POPUP_URL, popup_payload)

        resp_data = popup_body.get("respData") or popup_body.get("data") or {}
        item_list = resp_data.get("itemList") or resp_data.get("skuList") or popup_body.get("itemList") or []
        if not item_list:
            code = str(popup_body.get("respCode") or popup_body.get("code") or "")
            msg = popup_body.get("respMsg") or popup_body.get("errMsg") or popup_body.get("msg") or ""
            hint = f"（code={code} msg={msg}）" if (code or msg) else ""
            return False, f"商品不可{action_desc}{hint}"

        first = item_list[0]
        group_key = str(first.get("groupKey") or "")
        sku_id = str(first.get("skuId") or "")
        pre_fen = 0 if force_list else int(round(pre_price * 100))
        cur_fen = int(round(normalized_new_price * 100))
        change_item = {
            "groupKey": group_key,
            "skuId": sku_id,
            "productIds": [product_id],
            "prePrice": pre_fen,
            "curPrice": cur_fen,
        }

        check_payload = {"query": {"changePriceItems": [change_item], "confirmTips": []}}
        with self._lock:
            _resp, check_body = _post_json_with_retry(self._session, PRICE_CHECK_URL, check_payload)
        check_code = str(check_body.get("respCode") or check_body.get("code") or "0")
        if check_code not in ("0", "200"):
            return False, check_body.get("respMsg") or check_body.get("errMsg") or check_body.get("msg") or "价格预检失败"

        submit_payload = {
            "command": {
                "changePriceItems": [change_item],
                "confirmTips": [],
                "preChangePrice": False,
            }
        }
        submit_url = PRICE_CONFIRM_URL if force_list else PRICE_CHANGE_URL
        with self._lock:
            _resp, submit_body = _post_json_with_retry(self._session, submit_url, submit_payload)

        resp_data2 = submit_body.get("respData") or submit_body.get("data") or {}
        submit_code = str(submit_body.get("respCode") or submit_body.get("code") or "0")
        if submit_code not in ("0", "200") or resp_data2.get("successCount", 0) < 1:
            fail_msg = (
                resp_data2.get("errorMsg")
                or submit_body.get("errorMsg")
                or submit_body.get("errMsg")
                or submit_body.get("respMsg")
                or submit_body.get("msg")
                or "未知错误"
            )
            return False, fail_msg

        return True, resp_data2.get("tips") or ("上架成功" if force_list else "改价成功")

    def fetch_post_qc_intercepts(self, *, days: int = 2, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict]:
        today = datetime.date.today()
        day_set = {today - datetime.timedelta(days=offset) for offset in range(max(int(days), 1))}
        results: list[dict] = []
        seen_qc_codes: set[str] = set()
        page = 1
        statuses = ("80", "1")
        for status in statuses:
            page = 1
            while page <= MAX_FETCH_PAGES:
                body = self._merchant_product_list({
                    "pageNum": page,
                    "pageSize": page_size,
                    "tagIds": [],
                    "noTagIds": [],
                    "labels": [],
                    "statusList": [status],
                    "salesInShop": False,
                })
                data = _merchant_data(body)
                items = data.get("list", []) or []
                if not items:
                    break

                stop = False
                for item in items:
                    qc_code = str(item.get("qcCode") or "").strip()
                    if not qc_code or qc_code in seen_qc_codes:
                        continue
                    lifecycle = item.get("lifecycleTimes") or {}
                    intercept_time = _parse_datetime_value(lifecycle.get("applyReturnTime"))
                    sold_time = _parse_datetime_value(lifecycle.get("soldTime"))
                    time_anchor = intercept_time or sold_time
                    if time_anchor is None:
                        continue
                    anchor_day = time_anchor.date()
                    if anchor_day not in day_set:
                        continue

                    diff_items = self.query_post_qc_diff(qc_code=qc_code, product_id=str(item.get("productId") or ""))
                    if not diff_items:
                        continue

                    model = clean_model_name(item.get("spuName") or item.get("model") or item.get("title") or "")
                    for diff in diff_items:
                        post_result = str(diff.get("postQcResult") or "").strip()
                        if not post_result:
                            continue
                        lower_result = post_result.lower()
                        if any(flag in post_result for flag in ("正常", "无", "未检出", "几乎不可见")) and not any(
                            bad in post_result for bad in ("异常", "拆", "更换", "压伤", "泛黄", "泛红", "残影", "脏污", "轻微", "细微", "画面")
                        ):
                            continue
                        results.append({
                            "account_name": self.account_name,
                            "site": "YY",
                            "qc_code": qc_code,
                            "imei": str(item.get("imei") or ""),
                            "title": str(item.get("title") or ""),
                            "model": model,
                            "sold_time": lifecycle.get("soldTime") or "",
                            "apply_return_time": lifecycle.get("applyReturnTime") or "",
                            "event_time": lifecycle.get("applyReturnTime") or lifecycle.get("soldTime") or "",
                            "status_name": str(((item.get("state") or {}).get("statusName")) or ""),
                            "qc_item_name": str(diff.get("qcItemName") or ""),
                            "ori_qc_result": str(diff.get("oriQcResult") or ""),
                            "post_qc_result": str(diff.get("postQcResult") or ""),
                            "flawed_photos": list(diff.get("flawedPhotos") or []),
                            "product_id": str(item.get("productId") or ""),
                        })
                    seen_qc_codes.add(qc_code)

                if stop or len(items) < page_size:
                    break
                if _extract_total(data) and page * page_size >= _extract_total(data):
                    break
                page += 1
        return results

    def query_post_qc_diff(self, qc_code: str = "", product_id: str = "") -> list[dict]:
        qc = str(qc_code or "").strip()
        pid = str(product_id or "").strip()
        if not qc and not pid:
            return []

        payloads: list[dict] = []
        if pid:
            payloads.append({"productId": pid})
            payloads.append({"query": {"productId": pid}})
        if qc:
            payloads.append({"qcCode": qc})
            payloads.append({"query": {"qcCode": qc}})

        def _normalize_text(value: object) -> str:
            return str(value or "").strip()

        def _parse_pairs(text: str) -> dict[str, str]:
            pairs: dict[str, str] = {}
            for chunk in text.split(","):
                part = chunk.strip()
                if not part or ":" not in part:
                    continue
                key, value = part.split(":", 1)
                k = key.strip()
                v = value.strip()
                if not k:
                    continue
                pairs[k] = v
            return pairs

        def _expand_row(raw: dict, qc_item_id: str, qc_item_name: str, ori_result: str, post_result: str) -> list[dict]:
            ori_pairs = _parse_pairs(ori_result)
            post_pairs = _parse_pairs(post_result)
            if not ori_pairs and not post_pairs:
                return [{
                    **raw,
                    "qcItemId": qc_item_id,
                    "qcItemName": qc_item_name,
                    "oriQcResult": ori_result,
                    "postQcResult": post_result,
                }]

            rows: list[dict] = []
            pair_keys = set(ori_pairs.keys()) | set(post_pairs.keys())
            for pair_key in pair_keys:
                ori_val = _normalize_text(ori_pairs.get(pair_key))
                post_val = _normalize_text(post_pairs.get(pair_key))
                if not post_val:
                    continue
                if ori_val == post_val:
                    continue
                rows.append({
                    **raw,
                    "qcItemId": qc_item_id,
                    "qcItemName": f"{qc_item_name}/{pair_key}" if qc_item_name else pair_key,
                    "oriQcResult": ori_val,
                    "postQcResult": post_val,
                })
            return rows

        def _filtered_rows(candidates: list[dict]) -> list[dict]:
            dedup: dict[str, dict] = {}
            for raw in candidates:
                if not isinstance(raw, dict):
                    continue
                qc_item_id = _normalize_text(raw.get("qcItemId"))
                qc_item_name = _normalize_text(raw.get("qcItemName"))
                ori_result = _normalize_text(raw.get("oriQcResult"))
                post_result = _normalize_text(raw.get("postQcResult"))
                if not post_result:
                    continue
                expanded = _expand_row(raw, qc_item_id, qc_item_name, ori_result, post_result)
                for row in expanded:
                    key = "|".join([
                        _normalize_text(row.get("qcItemId")),
                        _normalize_text(row.get("qcItemName")),
                        _normalize_text(row.get("oriQcResult")),
                        _normalize_text(row.get("postQcResult")),
                    ])
                    if not key.strip("|"):
                        continue
                    dedup[key] = row
            return list(dedup.values())

        for payload in payloads:
            try:
                with self._lock:
                    _resp, body = _post_json_with_retry(self._session, self.POST_QC_DIFF_URL, payload)
            except Exception:
                continue
            data = body.get("respData") or body.get("data") or {}
            if isinstance(data, dict):
                intercept_state = bool(data.get("interceptState"))
                post_qc_has_diff = int(data.get("postQcHasDiff") or 0)
                candidates = data.get("postQcDiffItems")
                if not isinstance(candidates, list):
                    candidates = data.get("list")
                if isinstance(candidates, list):
                    rows = _filtered_rows(candidates)
                    if rows and (intercept_state or post_qc_has_diff == 1):
                        return rows
                    if rows:
                        return rows
            if isinstance(data, list):
                rows = _filtered_rows([x for x in data if isinstance(x, dict)])
                if rows:
                    return rows
        return []

    def _parse_product_detail(self, item: Optional[dict], fallback_code: str = "", fallback_status: str = "") -> Optional[ProductDetail]:
        if not item:
            return None

        properties = item.get("properties") or []
        props = {
            str(p.get("pnName") or ""): str(p.get("pvName") or "")
            for p in properties
            if isinstance(p, dict)
        }
        grade = item.get("gradeInfo") or {}
        price_info = item.get("priceInfo") or {}
        product_ids = item.get("productIds") or []
        product_id = item.get("productId") or (product_ids[0] if product_ids else "")

        lifecycle_times = item.get("lifecycleTimes") or {}
        listed_time = (
            _parse_datetime_value(lifecycle_times.get("putOnMartTime"))
            or _parse_datetime_value(item.get("listTime"))
            or _parse_datetime_value(item.get("createdTime"))
            or _parse_datetime_value(item.get("publishTime"))
        )

        raw_status, status_text = _extract_status(item, fallback_status=fallback_status)

        current_price_raw = price_info.get("sellingPrice")
        if current_price_raw in (None, ""):
            current_price_raw = item.get("price", 0)

        settle_price_raw = price_info.get("preSettlePrice")
        if settle_price_raw in (None, ""):
            settle_price_raw = item.get("settlePrice", 0)

        title = item.get("title", "")
        model_source = item.get("spuName") or item.get("model") or title
        model = clean_model_name(model_source) if model_source else ""
        condition = build_condition(properties, grade)
        if condition == "未知成色":
            condition = item.get("quality") or item.get("condition") or props.get("成色") or condition

        return ProductDetail(
            product_id=str(product_id),
            qc_code=str(item.get("qcCode", fallback_code)),
            title=title,
            model=model or title,
            condition=condition,
            capacity=item.get("storage", "") or item.get("capacity", "") or props.get("存储容量", "") or props.get("容量", ""),
            color=item.get("color", "") or props.get("颜色", ""),
            current_price=float(current_price_raw or 0) / 100,
            settle_price=float(settle_price_raw or 0) / 100,
            status=ProductStatus.from_raw(raw_status),
            status_text=status_text,
            listed_time=listed_time,
            imei=str(item.get("imei", "")),
            account_name=self.account_name,
            category_id=int(item.get("cateId") or item.get("categoryId") or 0),
            brand_id=int(item.get("brandId") or 0),
            model_id=int(item.get("modelId") or 0),
            product_params=_build_product_params(properties),
        )

    def query_official_reference_price(
        self,
        *,
        category_id: int,
        brand_id: int,
        model_id: int,
        product_params: str,
        condition: str = "",
    ) -> Optional[dict]:
        if category_id <= 0 or brand_id <= 0 or model_id <= 0 or not str(product_params or "").strip():
            return None
        payload = {
            "query": {
                "categoryId": int(category_id),
                "brandId": int(brand_id),
                "modelId": int(model_id),
                "productParams": str(product_params or "").strip(),
            }
        }
        try:
            with self._lock:
                _resp, body = _post_json_with_retry(self._session, self.DOUBLE_GRADE_PURCHASE_PRICE_URL, payload)
        except Exception:
            return None

        code = body.get("respCode")
        if code is None:
            code = body.get("code")
        if str(code or "0") not in ("0", "200"):
            return None

        resp_data = body.get("respData") or body.get("data") or {}
        infos = resp_data.get("referencePriceInfos") or []
        if not isinstance(infos, list) or not infos:
            return None

        target_condition = str(condition or "").strip()
        target_condition_compact = target_condition.replace("新", "") if target_condition else ""
        selected = None
        if target_condition:
            for info in infos:
                grade_name = str((info or {}).get("gradeName") or "").strip()
                if not grade_name:
                    continue
                if target_condition in grade_name or grade_name in target_condition:
                    selected = info
                    break
                compact_grade = grade_name.replace("新", "")
                if target_condition_compact and (target_condition_compact in compact_grade or compact_grade in target_condition_compact):
                    selected = info
                    break
        if selected is None:
            for info in infos:
                if not isinstance(info, dict):
                    continue
                if info.get("referencePrice") in (None, ""):
                    continue
                selected = info
                break
        if selected is None:
            return None

        raw_price = selected.get("referencePrice")
        raw_settle = selected.get("referenceSettlePrice")
        if raw_price in (None, "") and raw_settle in (None, ""):
            return None

        reference_price = None
        reference_settle_price = None
        try:
            if raw_price not in (None, ""):
                reference_price = float(raw_price) / 100
        except Exception:
            reference_price = None
        try:
            if raw_settle not in (None, ""):
                reference_settle_price = float(raw_settle) / 100
        except Exception:
            reference_settle_price = None

        if reference_price is None and reference_settle_price is None:
            return None

        return {
            "reference_price": reference_price,
            "reference_settle_price": reference_settle_price,
            "grade_name": str(selected.get("gradeName") or ""),
            "sku_id": str(selected.get("skuId") or ""),
        }


class DataFetcher:
    MERCHANT_LIST_URL = MERCHANT_LIST_URL

    def __init__(self, account_name: str, cookie: str):
        self.account_name = account_name
        self._session = _make_session(cookie)
        self._lock = threading.Lock()

    def fetch_sold(
        self,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        since: Optional[datetime.datetime] = None,
    ) -> Tuple[List[SoldRecord], int]:
        items, total = self._fetch_sold_page(page, page_size)
        records = [self._parse_sold(item) for item in items]
        filtered = [r for r in records if r is not None]
        if since is not None:
            filtered = [r for r in filtered if r.sold_time >= since]
        return filtered, total

    def fetch_all_sold(self, since: Optional[datetime.datetime] = None) -> List[SoldRecord]:
        all_records: list[SoldRecord] = []
        page = 1
        page_size = DEFAULT_PAGE_SIZE

        while page <= MAX_FETCH_PAGES:
            items, total = self._fetch_sold_page(page, page_size)
            if not items:
                break

            page_records: list[SoldRecord] = []
            all_older_than_since = True if since is not None else False
            for item in items:
                record = self._parse_sold(item)
                if record is None:
                    continue
                if since is not None and record.sold_time < since:
                    continue
                page_records.append(record)
                all_older_than_since = False

            all_records.extend(page_records)

            if len(items) < page_size:
                break
            if total and page * page_size >= total:
                break
            if since is not None and all_older_than_since:
                break
            page += 1

        return all_records

    def _fetch_sold_page(self, page: int, page_size: int) -> Tuple[list[dict], int]:
        payload = {
            "query": {
                "pageNum": page,
                "pageSize": page_size,
                "tagIds": [],
                "noTagIds": [],
                "labels": [],
                "statusList": ["80"],
                "salesInShop": False,
            }
        }
        try:
            with self._lock:
                _resp, body = _post_json_with_retry(self._session, self.MERCHANT_LIST_URL, payload)
        except Exception as e:
            raise RuntimeError(f"拉取成交记录失败: {e}") from e

        data = _merchant_data(body)
        total = _extract_total(data)
        return data.get("list", []) or [], total

    def _parse_sold(self, item: dict) -> Optional[SoldRecord]:
        try:
            properties = item.get("properties") or []
            props = {
                str(p.get("pnName") or ""): str(p.get("pvName") or "")
                for p in properties
                if isinstance(p, dict)
            }
            grade = item.get("gradeInfo") or {}
            lifecycle_times = item.get("lifecycleTimes") or {}
            sold_time = (
                _parse_datetime_value(lifecycle_times.get("soldTime"))
                or _parse_datetime_value(item.get("soldTime"))
                or _parse_datetime_value(item.get("dealTime"))
            )
            if sold_time is None:
                return None

            list_time = (
                _parse_datetime_value(lifecycle_times.get("putOnMartTime"))
                or _parse_datetime_value(item.get("listTime"))
                or _parse_datetime_value(item.get("publishTime"))
                or _parse_datetime_value(item.get("createdTime"))
            )
            hours_to_sell = None
            if list_time is not None:
                hours_to_sell = max(0.0, (sold_time - list_time).total_seconds() / 3600)

            price_info = item.get("priceInfo") or {}
            sold_price_raw = price_info.get("sellingPrice")
            if sold_price_raw in (None, ""):
                sold_price_raw = item.get("actualAmount")
            if sold_price_raw in (None, ""):
                sold_price_raw = item.get("price", 0)

            settle_price_raw = price_info.get("preSettlePrice")
            if settle_price_raw in (None, ""):
                settle_price_raw = item.get("settlePrice")

            product_ids = item.get("productIds") or []
            product_id = item.get("productId") or (product_ids[0] if product_ids else "")
            title = item.get("title", "")
            model_source = item.get("spuName") or item.get("model") or title
            model = clean_model_name(model_source) if model_source else ""
            condition = build_condition(properties, grade)
            if condition == "未知成色":
                condition = item.get("quality") or item.get("condition") or props.get("成色") or condition
            capacity = item.get("storage", "") or item.get("capacity", "") or props.get("存储容量", "") or props.get("容量", "")
            color = item.get("color", "") or props.get("颜色", "")

            return SoldRecord(
                product_id=str(product_id),
                title=title,
                sold_price=float(sold_price_raw or 0) / 100,
                sold_time=sold_time,
                hours_to_sell=hours_to_sell,
                source="zhuanzhuan",
                model=model or clean_model_name(title),
                condition=condition,
                capacity=capacity,
                color=color,
                list_time=list_time,
                settle_price=float(settle_price_raw or 0) / 100 if settle_price_raw not in (None, "") else None,
            )
        except Exception:
            return None
