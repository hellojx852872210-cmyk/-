"""
转转多店极速调价中枢 v1.0
"""

import os
import re
import json
import threading
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import pandas as pd
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import hashlib
import time as _time_mod
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─── 日志 ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────────────────
CONFIG_FILE = "zhuanzhuan_accounts.json"
CACHE_FILE  = "zhuanzhuan_sold_cache.csv"   # 历史成交数据本地缓存
API_URL           = "https://b.zhuanzhuan.com/gatewayapi/scm_render_product/merchantProductList"
API_IMEI_SEARCH   = API_URL   # 复用同一接口，通过 qcCodes 字段筛选
API_PRICE_POPUP   = "https://b.zhuanzhuan.com/gatewayapi/scm_render/newConfirmPricePopup"   # Step1: 获取 groupKey/skuId
API_PRICE_CHECK   = "https://b.zhuanzhuan.com/api/scm_render/merchantPriceCheck"            # Step2: 价格预检
API_PRICE_CHANGE  = "https://b.zhuanzhuan.com/api/scm_render/merchantChangePrice"           # Step3: 在架改价
API_PRICE_CONFIRM = "https://b.zhuanzhuan.com/gatewayapi/scm_render/merchantConfirmPrice"   # Step3: 未上架定价上架
API_MARKET_PRICE  = "https://b.zhuanzhuan.com/gatewayapi/scm_render/queryDoubleGradePurchasePrice"  # 行情参考价
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
PAGE_SIZE = 50
MAX_PAGES = 60          # 最多拉 3000 条/账号（days/count 模式）
MAX_PAGES_ALL = 200     # 全量模式最多拉 10000 条/账号
FAST_SALE_HOURS = 24          # 极速动销阈值（小时）
PLATFORM_FEE_RATE    = 0.05      # 平台扣点（示例，可按实际修改）
STATION_SERVICE_FEE  = 40        # 站点服务费（元），ERP settle_amount 已扣，转转 preSettlePrice 未扣

# ─── 爱管机 ERP 常量 ─────────────────────────────────────────────────────────
ERP_CONFIG_FILE      = "aiguanji_config.json"
ERP_BASE_URL         = "https://api.aiguanji.com"
ERP_PUT_SHELF_URL    = f"{ERP_BASE_URL}/api/v1/sale/put_shelf/index"   # 已上架
ERP_STOCK_URL        = f"{ERP_BASE_URL}/api/v1/storage/product/index"            # 在库库存
ERP_SOLD_URL         = f"{ERP_BASE_URL}/api/v1/sale/order/index"       # 已售订单
ERP_COST_MAP_FILE    = "erp_cost_map.json"  # 本地成本价缓存
ERP_COST_TAX_RATE    = 0.02                 # 成本税率：成本价 × 1.02 = 保本底价

# ─── 企业微信自建应用常量 ─────────────────────────────────────────────────────
WXAPP_CONFIG_FILE = "wxwork_app_config.json"  # 自建应用配置缓存
WXAPP_ACCESS_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
WXAPP_SEND_MSG_URL     = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
WXAPP_SERVER_PORT      = 8088   # 本地回调监听端口（企业微信→你的服务器→此端口）

# 品牌前缀 / 停用词（正则预编译）
_BRAND_PREFIX_RE = re.compile(
    r"^(苹果|Apple|华为|HUAWEI|小米|MI|OPPO|VIVO|Redmi|荣耀|Honor|realme)\s*",
    re.IGNORECASE,
)
_STOP_WORDS_RE = re.compile(
    r"\s+(128G?B?|256G?B?|512G?B?|1TB?|8\+|12\+|16\+|黑|白|金|银|青|紫|红|蓝|钛|"
    r"全网通|移动版|电信版|联通版|官换|港版|美版|双卡|5G|4G).*$",
    re.IGNORECASE,
)

# ─── 数据模型 ────────────────────────────────────────────────────────────────
@dataclass
class Account:
    name: str
    cookie: str

@dataclass
class SoldRecord:
    model: str
    condition: str
    capacity: str
    color: str
    list_time: Optional[datetime]
    sold_time: Optional[datetime]
    sell_price: float
    settle_price: float
    channel: str = "未知"  # 购买渠道（国行/港澳台版/其他版本），默认未知


@dataclass
class ProductDetail:
    """通过质检码查询到的在售商品信息"""
    product_id: str
    sku_id: str
    group_key: str
    title: str
    model: str
    condition: str
    capacity: str
    color: str
    current_price: float
    settle_price: float
    status: str


@dataclass
class BatchItem:
    """批量调价列表中的一行"""
    product_id:      str
    title:           str
    model:           str
    condition:       str
    capacity:        str
    color:           str
    current_price:   float
    settle_price:    float        # 当前预计到手价（元），直接来自 API
    suggested_price: int          # 0 表示无匹配
    is_fast:         bool
    status_code:     str  = ""
    status_name:     str  = ""
    qc_code:         str  = ""
    imei:            str  = ""
    checked:         bool = True
    low_confidence:  bool = False  # 样本不足或颜色无精确匹配
    floor_price:     int  = 0      # 底价预警（统计P20，fallback）
    cost_floor:      int  = 0      # 成本底价（ERP cost_price×1.02，优先级高于P20）
    # ── 行情查询所需字段（来自转转原始数据）──────────────────────────────
    category_id:     int  = 0      # cateId
    brand_id:        int  = 0      # brandId
    model_id:        int  = 0      # modelId
    grade_name:      str  = ""     # 成色名称（如 "95·A"）
    product_params:  str  = ""     # properties 拼接字符串，用于行情查询


# ─── 成色字符串构建（统一从properties提取）────────────────────────────────
def _build_product_params(properties: list) -> str:
    """
    将 properties 数组拼成行情接口所需的 productParams 字符串。
    格式：pnId1:pvId1;pnId2:pvId2;...
    pvId 为 "null" 时跳过该属性。
    """
    parts = []
    for p in properties:
        pn_id = str(p.get("pnId") or "")
        pv_id = str(p.get("pvId") or "")
        if pn_id and pv_id and pv_id != "null":
            parts.append(f"{pn_id}:{pv_id}")
    return ";".join(parts) + ";" if parts else ""


def build_condition(properties: list, grade: dict = None) -> str:
    """
    统一从properties数组中提取成色信息
    properties: [{"pnName": "成色", "pvName": "95新·A"}, ...]
    """
    if not properties:
        return "未知成色"
    
    for prop in properties:
        if prop.get("pnName") == "成色":
            condition = (prop.get("pvName") or "").strip()
            if condition:
                # 直接返回原始值，不做格式转换
                # 转转API返回的格式可能是: "95新·A", "99新·S", "9成新·B" 等
                return condition
    
    return "未知成色"


# ─── 爱管机 ERP 对接 ──────────────────────────────────────────────────────────
class ErpConfig:
    """爱管机 Authorization token + Version 的存取"""

    def __init__(self, path: str = ERP_CONFIG_FILE):
        self.path  = path
        data = self._load()
        self.token:   str = data.get("token", "")
        self.version: str = data.get("version", "")

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, token: str, version: str = ""):
        self.token   = token.strip()
        self.version = version.strip() if version else self.version
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"token": self.token, "version": self.version},
                      f, ensure_ascii=False)


class ErpFetcher:
    """
    从爱管机 ERP 拉取商品数据。
    支持：已上架商品 / 在库库存 / 已售订单
    """

    def __init__(self, token: str, version: str = ""):
        self.headers = {
            "Authorization": token,
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/plain, */*",
            "User-Agent":    USER_AGENT,
            "Origin":        "https://saas.aiguanji.com",
            "Referer":       "https://saas.aiguanji.com/",
            "Version":       version or datetime.now().strftime("%Y%m%d") + "01",
        }

    @classmethod
    def refresh_version(cls) -> str:
        return ""  # 保留兼容，不再用自动抓取

    # ── 通用分页拉取 ─────────────────────────────────────────────────────────
    def _fetch_all(self, url: str, payload: dict, on_log=None) -> list[dict]:
        all_items: list[dict] = []
        page = 1
        _retried_version = False

        while True:
            payload["page"] = page
            try:
                resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
                # 先尝试解析 body，不管 HTTP 状态码
                try:
                    body = resp.json()
                except Exception:
                    resp.raise_for_status()   # JSON 解析失败才抛 HTTP 错误
                    raise RuntimeError(f"第{page}页响应无法解析")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"第{page}页请求失败：{e}")

            code = body.get("code")
            msg  = body.get("msg", "")

            # ── 版本更新：刷新 Version 头并重试一次 ──────────────────────
            if str(code) != "0" and "新版本" in str(msg) and not _retried_version:
                new_ver = ErpFetcher.refresh_version()
                self.headers["Version"] = new_ver
                if on_log:
                    on_log(f"  ⚡ 检测到版本更新，已刷新为 {new_ver}，重试...")
                _retried_version = True
                continue

            if str(code) != "0":
                raise RuntimeError(f"ERP 返回错误 code={code!r}：{msg or str(body)[:200]}")

            outer = body.get("data", {})
            if isinstance(outer, dict):
                inner = outer.get("data") or []
            elif isinstance(outer, list):
                inner = outer
            else:
                inner = []

            if not inner:
                break

            all_items.extend(inner)
            if on_log:
                on_log(f"  已拉取 {len(all_items)} 条…")

            last_page = outer.get("last_page", 1) if isinstance(outer, dict) else 1
            per_page  = outer.get("per_page", payload.get("limit", 1000)) if isinstance(outer, dict) else 1000
            if page >= last_page or len(inner) < per_page:
                break
            page += 1
            _retried_version = False
        return all_items

    # ── 已上架商品 ───────────────────────────────────────────────────────────
    def fetch_put_shelf(self, on_log=None) -> list[dict]:
        payload = {
            "page": 1, "limit": 1000,
            "value1": "", "cate_id": "", "brand_id": "",
            "product_id": "", "sale_channel_id": "", "selected_attr": {},
        }
        return self._fetch_all(ERP_PUT_SHELF_URL, payload, on_log)

    # ── 在库库存 ─────────────────────────────────────────────────────────────
    def fetch_stock(self, on_log=None) -> list[dict]:
        payload = {
            "page": 1, "limit": 1000,
            "value1": "", "cate_id": "", "brand_id": "",
            "product_id": "", "warehouse_id": "", "selected_attr": {},
        }
        try:
            return self._fetch_all(ERP_STOCK_URL, payload, on_log)
        except Exception as e:
            if on_log: on_log(f"  ⚠️ 在库接口暂不可用（{e}），跳过")
            return []

    # ── 已售订单 ─────────────────────────────────────────────────────────────
    def fetch_sold(self, on_log=None) -> list[dict]:
        payload = {
            "page": 1, "limit": 1000,
            "value1": "", "cate_id": "", "brand_id": "",
            "product_id": "", "sale_channel_id": "", "selected_attr": {},
        }
        try:
            return self._fetch_all(ERP_SOLD_URL, payload, on_log)
        except Exception as e:
            if on_log: on_log(f"  ⚠️ 已售接口暂不可用（{e}），跳过")
            return []

    # ── 拉取成本价：从在库+已上架合并提取 (型号,颜色,内存)→成本价 ─────────────
    def fetch_cost_prices(self, on_log=None) -> dict[str, float]:
        """
        拉取 ERP 在库库存和已上架商品，提取成本价（cost_price 字段）。
        返回 {key: cost_price}，key = "型号|颜色|内存"（用于本地缓存）。
        同一 key 取所有记录的中位数，减少单台异常值影响。
        """
        def log(m):
            if on_log: on_log(m)

        all_items: list[dict] = []
        log("📥 拉取在库库存成本价...")
        try:
            stock = self.fetch_stock(on_log=on_log)
            all_items.extend(stock)
            log(f"  在库：{len(stock)} 条")
        except Exception as e:
            log(f"  ⚠️ 在库拉取失败：{e}")

        log("📥 拉取已上架成本价...")
        try:
            shelf = self.fetch_put_shelf(on_log=on_log)
            all_items.extend(shelf)
            log(f"  已上架：{len(shelf)} 条")
        except Exception as e:
            log(f"  ⚠️ 已上架拉取失败：{e}")

        if not all_items:
            raise RuntimeError("未拉取到任何数据，请检查 Token 和 Version")

        # 按 key 收集所有成本价
        from collections import defaultdict
        cost_lists: dict[str, list[float]] = defaultdict(list)

        for it in all_items:
            # cost_price 字段（成本价，非采购价）
            raw_cost = it.get("cost_price") or it.get("cost") or 0
            try:
                cost = float(raw_cost)
            except Exception:
                continue
            if cost <= 0:
                continue

            name  = it.get("name") or ""
            attrs = it.get("selected_attr") or {}
            memory = (attrs.get("内存") or attrs.get("容量")
                      or it.get("product_attr_memory") or "")
            color  = (attrs.get("颜色") or it.get("product_attr_color") or "")
            model  = _BRAND_PREFIX_RE.sub("", name).strip()
            if not model:
                continue

            key = f"{model}|{color}|{memory}"
            cost_lists[key].append(cost)

        # 取中位数，加 2% 税得到保本底价
        result: dict[str, float] = {}
        for key, costs in cost_lists.items():
            costs.sort()
            median_cost = costs[len(costs) // 2]
            result[key] = round(median_cost * (1 + ERP_COST_TAX_RATE), 2)

        log(f"✅ 共提取 {len(result)} 个 (型号+颜色+内存) 组合的成本底价")
        return result

    # ── 将 ERP raw item 转为 qc_code / imei 列表 ────────────────────────────
    @staticmethod
    def extract_codes(items: list[dict]) -> list[str]:
        """提取质检码（sale_no）或 IMEI，过滤空值，去重"""
        seen: set[str] = set()
        result: list[str] = []
        for it in items:
            code = str(it.get("sale_no") or "").strip()
            if not code:
                code = str(it.get("imei") or "").strip()
            if code and code not in seen:
                seen.add(code)
                result.append(code)
        return result

    # ── 校验 token ───────────────────────────────────────────────────────────
    def check_token(self) -> tuple[bool, str]:
        try:
            resp = requests.post(
                ERP_PUT_SHELF_URL,
                json={"page": 1, "limit": 1, "value1": "", "cate_id": "",
                      "brand_id": "", "product_id": "", "sale_channel_id": "",
                      "selected_attr": {}},
                headers=self.headers, timeout=8,
            )
            body = resp.json()
            if body.get("code") == 0:
                return True, "Token 有效"
            return False, body.get("msg", "未知错误")
        except Exception as e:
            return False, str(e)


# ─── 企业微信自建应用：配置 + Token管理 + 消息发送 ──────────────────────────────
class WxAppConfig:
    """企业微信自建应用配置持久化"""
    def __init__(self):
        self.corp_id   = ""
        self.agent_id  = ""
        self.secret    = ""
        self.token     = ""          # 消息回调校验 Token（自定义字符串）
        self.aes_key   = ""          # EncodingAESKey（43位，企业微信管理台生成）
        self.ngrok_authtoken = ""    # pyngrok authtoken（自动管理隧道）
        self.ngrok_url = ""          # 最近一次成功拿到的公网URL（自动写入）
        self._load()

    def _load(self):
        try:
            d = json.loads(open(WXAPP_CONFIG_FILE, encoding="utf-8").read())
            for k in ("corp_id","agent_id","secret","token","aes_key","ngrok_authtoken","ngrok_url"):
                setattr(self, k, d.get(k, ""))
        except Exception:
            pass

    def save(self):
        with open(WXAPP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({k: getattr(self, k) for k in
                       ("corp_id","agent_id","secret","token","aes_key","ngrok_authtoken","ngrok_url")},
                      f, ensure_ascii=False, indent=2)

    @property
    def is_configured(self) -> bool:
        return bool(self.corp_id and self.agent_id and self.secret)


class WxAppClient:
    """
    企业微信自建应用：AccessToken管理 + 发送消息。
    AccessToken 有效期2小时，自动刷新缓存。
    """
    def __init__(self, config: WxAppConfig):
        self.cfg = config
        self._token_cache: tuple[str, float] = ("", 0.0)  # (token, expire_ts)

    def get_access_token(self) -> str:
        token, expire_ts = self._token_cache
        if token and _time_mod.time() < expire_ts - 60:
            return token
        resp = requests.get(WXAPP_ACCESS_TOKEN_URL, params={
            "corpid":     self.cfg.corp_id,
            "corpsecret": self.cfg.secret,
        }, timeout=10)
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"获取AccessToken失败：{data.get('errmsg')}")
        token = data["access_token"]
        self._token_cache = (token, _time_mod.time() + data.get("expires_in", 7200))
        return token

    def send_text(self, content: str, to_user: str = "@all") -> bool:
        """向指定用户（或全员）发文本消息，返回是否成功"""
        try:
            token = self.get_access_token()
            resp  = requests.post(
                WXAPP_SEND_MSG_URL,
                params={"access_token": token},
                json={
                    "touser":  to_user,
                    "msgtype": "text",
                    "agentid": int(self.cfg.agent_id),
                    "text":    {"content": content},
                },
                timeout=10,
            )
            result = resp.json()
            return result.get("errcode", -1) == 0
        except Exception as e:
            logger.warning("企业微信发送消息失败：%s", e)
            return False

    def verify_signature(self, signature: str, timestamp: str,
                          nonce: str, echostr: str = "") -> bool:
        """验证企业微信回调签名（GET 验证阶段）"""
        token = self.cfg.token

        # 企业微信签名算法：
        # 1. 将token、timestamp、nonce三个参数进行字典序排序
        # 2. 将三个参数字符串拼接成一个字符串进行sha1加密
        # 3. 开发者获得加密后的字符串可与signature对比，标识该请求来源于微信

        # 注意：验证URL时不包含echostr！
        items = sorted([token, timestamp, nonce])
        sha1_str = "".join(items)
        calculated_sig = hashlib.sha1(sha1_str.encode()).hexdigest()

        # 调试日志
        logger.info(f"签名验证: token={token}, timestamp={timestamp}, nonce={nonce[:10]}...")
        logger.info(f"计算的签名: {calculated_sig}")
        logger.info(f"收到的签名: {signature}")
        logger.info(f"验证结果: {calculated_sig == signature}")

        return calculated_sig == signature


class WxAppCommandDispatcher:
    """
    指令解析与分发。
    注册的处理函数签名：fn(sender_id: str, args: str) -> str（返回回复文本）
    """
    def __init__(self):
        self._handlers: dict[str, callable] = {}
        self._aliases:  dict[str, str]      = {}
        self._default_handler: callable = None

    def register(self, keyword: str, fn, *aliases):
        """注册指令处理函数，支持多别名"""
        self._handlers[keyword] = fn
        for a in aliases:
            self._aliases[a] = keyword

    def set_default_handler(self, fn):
        """设置默认处理器，当没有匹配的命令时调用"""
        self._default_handler = fn

    def dispatch(self, sender_id: str, text: str) -> str:
        text    = text.strip()
        kw, _, args = text.partition(" ")
        kw = self._aliases.get(kw, kw)
        fn = self._handlers.get(kw)
        if fn:
            try:
                return fn(sender_id, args.strip())
            except Exception as e:
                return f"❌ 指令执行出错：{e}"

        # 未识别 → 如果有默认处理器，使用默认处理器
        if self._default_handler:
            try:
                return self._default_handler(sender_id, text)
            except Exception as e:
                return f"❌ 处理出错：{e}"

        # 没有默认处理器 → 返回帮助
        cmds = list(self._handlers.keys())
        cmd_list = "\n".join(f"  · {c}" for c in cmds)
        return f"🤖 未识别指令\u300c{kw}\u300d\n\n可用指令：\n{cmd_list}\n\n发送\u300c帮助\u300d查看详细说明"

    @property
    def all_commands(self) -> list[str]:
        return list(self._handlers.keys())


# ─── 成本底价表 ───────────────────────────────────────────────────────────────
class CostPriceMap:
    """
    本地成本价缓存：从 ERP 拉取，存为 JSON。
    key = "型号|颜色|内存"，value = 成本价×1.02（含税保本线）。
    查询时返回 float，无匹配返回 None。
    """

    def __init__(self, path: str = ERP_COST_MAP_FILE):
        self.path = path
        self._map: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, data: dict[str, float]):
        self._map = data
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("成本价缓存保存失败：%s", e)

    def get(self, model: str, color: str, memory: str) -> Optional[float]:
        """严格匹配，无匹配返回 None"""
        key = f"{model}|{color}|{memory}"
        return self._map.get(key)

    def get_fuzzy(self, model: str, color: str, memory: str) -> Optional[float]:
        """先严格，再忽略颜色降级匹配"""
        v = self.get(model, color, memory)
        if v is not None:
            return v
        # 颜色降级：遍历找型号+内存相同的第一个
        prefix = f"{model}|"
        suffix = f"|{memory}"
        candidates = [
            val for k, val in self._map.items()
            if k.startswith(prefix) and k.endswith(suffix)
        ]
        if candidates:
            candidates.sort()
            return candidates[len(candidates) // 2]   # 取中位
        return None

    @property
    def count(self) -> int:
        return len(self._map)

    @property
    def is_empty(self) -> bool:
        return not self._map


# ─── 本地缓存存取 ─────────────────────────────────────────────────────────────
class DataStore:
    """
    把历史成交数据持久化到本地 CSV，支持：
      - load()        → 读取缓存，返回 DataFrame（空则返回空 DF）
      - save(df)      → 覆盖写入
      - latest_date() → 返回缓存中最新的售出日期（None 表示无缓存）
      - merge(old_df, new_df) → 合并去重，按售出时间排序
    """
    COLS = ["型号", "精确成色", "容量", "颜色", "购买渠道", "上架时间", "售出时间",
            "最终售价", "预计最低结算价", "sales_hours"]

    def __init__(self, path: str = CACHE_FILE):
        self.path = path

    def load(self) -> pd.DataFrame:
        if not os.path.exists(self.path):
            return pd.DataFrame(columns=self.COLS)
        try:
            df = pd.read_csv(self.path, parse_dates=["上架时间", "售出时间"])
            if "sales_hours" not in df.columns:
                df["sales_hours"] = (
                    (df["售出时间"] - df["上架时间"]).dt.total_seconds() / 3600
                )
            # 兼容旧数据：如果没有购买渠道列，添加默认值
            if "购买渠道" not in df.columns:
                df["购买渠道"] = "未知"
            logger.info("缓存加载 %d 条", len(df))
            return df
        except Exception as e:
            logger.warning("缓存读取失败，重置: %s", e)
            return pd.DataFrame(columns=self.COLS)

    def save(self, df: pd.DataFrame):
        df.to_csv(self.path, index=False, encoding="utf-8-sig")
        logger.info("缓存已保存 %d 条 → %s", len(df), self.path)

    def latest_date(self) -> Optional[pd.Timestamp]:
        df = self.load()
        if df.empty or "售出时间" not in df.columns:
            return None
        ts = pd.to_datetime(df["售出时间"]).max()
        return ts if pd.notna(ts) else None

    @staticmethod
    def merge(old_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
        if old_df.empty:
            return new_df.reset_index(drop=True)
        if new_df.empty:
            return old_df.reset_index(drop=True)
        combined = pd.concat([old_df, new_df], ignore_index=True)
        # 去重：同一商品同一售出时间视为重复
        combined = combined.drop_duplicates(
            subset=["型号", "精确成色", "容量", "颜色", "售出时间"]
        )
        return combined.sort_values("售出时间", ascending=False).reset_index(drop=True)


# ─── 账号管理 ────────────────────────────────────────────────────────────────
class AccountManager:
    def __init__(self, config_path: str = CONFIG_FILE):
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
            json.dump([asdict(a) for a in self.accounts], f, ensure_ascii=False, indent=4)

    def add(self, name: str, cookie: str):
        if not name or not cookie:
            raise ValueError("店名和 Cookie 不能为空")
        if any(a.name == name for a in self.accounts):
            raise ValueError(f"店铺 '{name}' 已存在")
        self.accounts.append(Account(name=name, cookie=cookie))
        self.save()

    def remove(self, name: str):
        self.accounts = [a for a in self.accounts if a.name != name]
        self.save()


# ─── 型号清洗 ────────────────────────────────────────────────────────────────
def clean_model_name(title: str) -> str:
    """去掉品牌前缀、规格后缀，提取核心型号"""
    t = _BRAND_PREFIX_RE.sub("", title.strip())
    t = _STOP_WORDS_RE.sub("", t)
    return t.strip() or title.strip()


# ─── IMEI / 质检码服务 ───────────────────────────────────────────────────────
class ImeiService:
    """
    负责两件事：
      1. lookup()  —— 用 IMEI 或质检码查询平台上的在售商品信息
      2. update_price() —— 对指定商品提交改价请求
    """

    def __init__(self, cookie: str):
        self.headers = {
            "cookie": cookie,
            "user-agent": USER_AGENT,
            "content-type": "application/json",
        }

    # ── 查询 ──────────────────────────────────────────────────────────────────
    def lookup(self, code: str) -> ProductDetail:
        """
        支持质检码和 IMEI（15位纯数字）两种输入：
        - IMEI  → query.imeis 字段
        - 质检码 → query.qcCodes 字段
        两种查询失败时自动互换重试一次。
        """
        code = code.strip()
        is_imei = bool(re.fullmatch(r"\d{15}", code))

        def _do_query(use_imei: bool) -> list[dict]:
            key     = "imeis" if use_imei else "qcCodes"
            payload = {"query": {
                "pageNum": 1, "pageSize": 50,
                "tagIds": [], "noTagIds": [], "labels": [],
                key: [code],
                "statusList": ["0"], "salesInShop": False,
            }}
            resp = requests.post(API_IMEI_SEARCH, json=payload,
                                 headers=self.headers, timeout=10)
            resp.raise_for_status()
            body = resp.json()
            return (
                (body.get("data") or {}).get("list")
                or (body.get("respData") or {}).get("list")
                or []
            )

        items = _do_query(is_imei)
        # 如果第一次无结果，换字段重试一次
        if not items:
            items = _do_query(not is_imei)
        if not items:
            label = "IMEI" if is_imei else "质检码"
            raise ValueError(f"未找到{label}「{code}」对应的在售商品，请确认编码正确且商品在架")

        item = items[0]
        props   = {p["pnName"]: p["pvName"] for p in (item.get("properties") or [])}
        grade   = item.get("gradeInfo") or {}
        p_info  = item.get("priceInfo") or {}

        product_ids = item.get("productIds") or [str(item.get("productId") or "")]

        return ProductDetail(
            product_id    = product_ids[0] if product_ids else "",
            sku_id        = str(item.get("skuId") or ""),
            group_key     = str(item.get("groupKey") or ""),
            title         = item.get("title") or "",
            model         = clean_model_name(item.get("title") or ""),
            condition     = build_condition(item.get("properties", []), grade),
            capacity      = props.get("存储容量") or props.get("容量") or "未知",
            color         = props.get("颜色") or "未知",
            current_price = (p_info.get("sellingPrice") or 0) / 100,
            settle_price  = (p_info.get("preSettlePrice") or 0) / 100,
            status        = item.get("statusDesc") or str(item.get("status") or "未知"),
        )

    # ── 拉取全部在售商品 ──────────────────────────────────────────────────────
    def fetch_settle_price(self, product_id: str) -> Optional[float]:
        """
        改价成功后立刻调用，从转转接口重新拿 preSettlePrice。
        返回元为单位的到手价，失败返回 None（调用方降级展示估算值）。
        """
        try:
            resp = requests.post(
                API_URL,
                json={"query": {
                    "pageNum": 1, "pageSize": 20,
                    "tagIds": [], "noTagIds": [], "labels": [],
                    "statusList": ["0", "60"],   # 在架+未上架都查
                    "salesInShop": False,
                    "productIds": [product_id],
                }},
                headers=self.headers, timeout=8,
            )
            body = resp.json()
            items = (
                (body.get("data") or {}).get("list")
                or (body.get("respData") or {}).get("list")
                or []
            )
            for it in items:
                if str(it.get("productId") or "") == str(product_id):
                    p_info = it.get("priceInfo") or {}
                    raw = p_info.get("preSettlePrice")
                    if raw:
                        return float(raw) / 100
            return None
        except Exception:
            return None

    def check_cookie_valid(self) -> tuple[bool, str]:
        """
        轻量检测 cookie 是否有效。
        返回 (True, "") 或 (False, 原因)
        """
        try:
            resp = requests.post(
                API_URL,
                json={"query": {"pageNum": 1, "pageSize": 1, "statusList": ["0"],
                                "tagIds": [], "noTagIds": [], "labels": [],
                                "salesInShop": False}},
                headers=self.headers, timeout=8,
            )
            body = resp.json()
            code = body.get("code") or body.get("respCode") or 0
            msg  = body.get("msg") or body.get("respMsg") or ""
            # 未登录 / token 失效的常见标志
            if resp.status_code in (401, 403):
                return False, "HTTP 状态码异常"
            if str(code) in ("401", "403", "-1", "10000", "200001"):
                return False, msg or "未登录"
            low = msg.lower()
            if any(k in low for k in ("login", "未登录", "登录", "expired", "invalid", "token")):
                return False, msg
            return True, ""
        except Exception as e:
            return False, str(e)


    def fetch_on_sale(self, on_progress=None, status_list=None) -> list[dict]:
        """
        分页拉取账号下商品。statusList ["0"] = 在架（已验证可用）。
        """
        all_items = []
        query_base: dict = {
            "pageNum":    1,
            "pageSize":   PAGE_SIZE,
            "tagIds":     [],
            "noTagIds":   [],
            "labels":     [],
            "statusList": status_list if status_list is not None else ["0"],
            "salesInShop": False,
        }
        for page in range(1, MAX_PAGES + 1):
            if on_progress:
                on_progress(page, len(all_items))
            try:
                query = {**query_base, "pageNum": page}
                resp = requests.post(
                    API_URL, json={"query": query},
                    headers=self.headers, timeout=10,
                )
                resp.raise_for_status()
                body = resp.json()
            except requests.RequestException as e:
                raise requests.RequestException(f"第{page}页网络异常: {e}")

            items = (
                (body.get("data") or {}).get("list")
                or (body.get("respData") or {}).get("list")
                or []
            )
            # 调试：第一页若为空打印完整响应
            if page == 1 and not items:
                logger.warning("fetch_on_sale 第1页返回空，完整响应: %s", body)
            if not items:
                break
            all_items.extend(items)
            if len(items) < PAGE_SIZE:
                break

        return all_items

    # ── 按质检码列表批量查询 ──────────────────────────────────────────────────
    def fetch_by_codes(self, codes: list[str], on_progress=None,
                       status_list: list[str] = None) -> tuple[list[dict], list[str]]:
        """
        批量按质检码或 IMEI 查询商品。自动识别：15 位纯数字 → imeis 字段，其余 → qcCodes 字段。
        每次最多传 50 个，分批请求。
        注意：转转API的 statusList 只支持单个值，多个状态会自动拆分为多次请求合并结果。
        status_list: 默认 ["0"]（在架）；传 ["0","60","80"] 可同时查未上架和已售。
        返回 (成功的 raw item 列表, 未找到的 code 列表)
        """
        found_items: list[dict] = []
        not_found:   list[str]  = []
        batch_size   = 50
        _status_list = status_list if status_list is not None else ["0"]
        _imei_re     = re.compile(r"\d{15}")

        def _query_one_status(field_key: str, sub_batch: list, status: str) -> list[dict]:
            """对单个 status 发起一次请求，返回 items 列表"""
            try:
                resp = requests.post(
                    API_URL,
                    json={"query": {
                        "pageNum": 1, "pageSize": batch_size,
                        "tagIds": [], "noTagIds": [], "labels": [],
                        field_key: sub_batch,
                        "statusList": [status], "salesInShop": False,
                    }},
                    headers=self.headers, timeout=10,
                )
                resp.raise_for_status()
                body = resp.json()
            except requests.RequestException as e:
                raise requests.RequestException(f"网络异常: {e}")

            resp_code = body.get("respCode") or body.get("code")
            resp_msg  = body.get("respMsg") or body.get("errMsg") or body.get("msg", "")
            if resp_code and str(resp_code) not in ("0", "200"):
                logger.warning(f"转转API错误: code={resp_code}, msg={resp_msg}, field={field_key}, status={status}, count={len(sub_batch)}")
                return []
            return (
                (body.get("data") or {}).get("list")
                or (body.get("respData") or {}).get("list")
                or []
            )

        for i in range(0, len(codes), batch_size):
            batch = codes[i: i + batch_size]
            if on_progress:
                on_progress(i, len(codes))

            imei_batch = [c for c in batch if _imei_re.fullmatch(c)]
            qc_batch   = [c for c in batch if not _imei_re.fullmatch(c)]

            batch_found: list[dict] = []
            seen_product_ids: set = set()

            for field_key, sub_batch in [("imeis", imei_batch), ("qcCodes", qc_batch)]:
                if not sub_batch:
                    continue
                # 对每个 status 单独请求，避免 API "参数异常"
                for status in _status_list:
                    try:
                        items = _query_one_status(field_key, sub_batch, status)
                    except requests.RequestException as e:
                        raise requests.RequestException(f"批量查询第{i//batch_size+1}批网络异常: {e}")
                    for it in items:
                        pid = it.get("productId") or it.get("qcCode") or it.get("imei")
                        if pid and pid not in seen_product_ids:
                            seen_product_ids.add(pid)
                            batch_found.append(it)

            found_items.extend(batch_found)

            found_qc   = {str(it.get("qcCode") or "") for it in batch_found}
            found_imei = {str(it.get("imei")   or "") for it in batch_found}
            for code in batch:
                if code not in found_qc and code not in found_imei:
                    not_found.append(code)

        return found_items, not_found

    # ── 改价 / 定价上架 ───────────────────────────────────────────────────────
    def update_price(self, product: "ProductDetail", new_price: float) -> str:
        """
        自动判断两条路径：
          • 在架（status=60 以外）→ merchantChangePrice
          • 未上架（status=60）   → merchantConfirmPrice，prePrice=0 触发上架
        """
        if new_price <= 0:
            raise ValueError("新价格必须大于 0")

        is_unlisted = (getattr(product, "status", "") in ("60", "未上架"))
        pre_fen     = 0 if is_unlisted else int(round(product.current_price * 100))
        cur_fen     = int(round(new_price * 100))
        action_desc = "定价上架" if is_unlisted else "改价"

        # ── Step 1: 获取 groupKey / skuId ─────────────────────────────────
        popup_resp = requests.post(
            API_PRICE_POPUP,
            json={"confirmPricePopupQuery": {
                "productIds": [product.product_id],
                "groupBySku": 1,
            }},
            headers=self.headers, timeout=10,
        )
        popup_resp.raise_for_status()
        popup_body = popup_resp.json()

        resp_data = popup_body.get("respData") or popup_body.get("data") or {}
        item_list = (
            resp_data.get("itemList")
            or resp_data.get("skuList")
            or popup_body.get("itemList")
            or []
        )
        if not item_list:
            code = str(popup_body.get("respCode") or popup_body.get("code") or "")
            msg  = popup_body.get("respMsg") or popup_body.get("errMsg") or ""
            hint = f"（code={code} msg={msg}）" if (code or msg) else ""
            raise ValueError(f"商品不可{action_desc}{hint}，请在转转后台确认状态")

        first     = item_list[0]
        group_key = str(first.get("groupKey") or "")
        sku_id    = str(first.get("skuId") or "")

        change_item = {
            "groupKey":   group_key,
            "skuId":      sku_id,
            "productIds": [product.product_id],
            "prePrice":   pre_fen,
            "curPrice":   cur_fen,
        }

        # ── Step 2: 价格预检 ───────────────────────────────────────────────
        check_resp = requests.post(
            API_PRICE_CHECK,
            json={"query": {"changePriceItems": [change_item], "confirmTips": []}},
            headers=self.headers, timeout=10,
        )
        check_resp.raise_for_status()
        check_body = check_resp.json()
        if str(check_body.get("respCode", "")) not in ("0", "200"):
            raise ValueError(check_body.get("respMsg") or check_body.get("errMsg") or "价格预检失败")

        # ── Step 3: 提交（在架改价 vs 未上架定价，接口不同）──────────────
        submit_url = API_PRICE_CONFIRM if is_unlisted else API_PRICE_CHANGE
        submit_resp = requests.post(
            submit_url,
            json={"command": {
                "changePriceItems": [change_item],
                "confirmTips":      [],
                "preChangePrice":   False,
            }},
            headers=self.headers, timeout=10,
        )
        submit_resp.raise_for_status()
        submit_body = submit_resp.json()

        resp_data2 = submit_body.get("respData") or submit_body.get("data") or {}
        if str(submit_body.get("respCode", "")) not in ("0", "200") or resp_data2.get("successCount", 0) < 1:
            fail_msg = (
                resp_data2.get("errorMsg")
                or submit_body.get("errorMsg")
                or submit_body.get("errMsg")
                or "未知错误"
            )
            raise ValueError(f"{action_desc}失败：{fail_msg}")

        return resp_data2.get("tips") or f"{action_desc}成功"


# ─── 数据拉取 ────────────────────────────────────────────────────────────────
class DataFetcher:
    """负责从转转 API 拉取已售商品数据，通过回调汇报进度"""

    def __init__(
        self,
        accounts: list[Account],
        mode: str,           # "days" | "count" | "incremental"
        limit: int,
        on_progress,
        on_log,
        on_done,
        on_error,
        since_date: Optional[pd.Timestamp] = None,  # 增量模式：只拉此日期之后的数据
    ):
        self.accounts   = accounts
        self.mode       = mode
        self.limit      = limit
        self.on_progress = on_progress
        self.on_log     = on_log
        self.on_done    = on_done
        self.on_error   = on_error
        self.since_date = since_date

    def _parse_item(self, item: dict) -> Optional[SoldRecord]:
        times = item.get("lifecycleTimes") or {}
        sold_time_raw = times.get("soldTime")
        if not sold_time_raw:
            return None
        try:
            sold_time = pd.to_datetime(sold_time_raw)
            list_time_raw = times.get("putOnMartTime")
            list_time = pd.to_datetime(list_time_raw) if list_time_raw else None
        except Exception:
            return None

        props = {p["pnName"]: p["pvName"] for p in item.get("properties", []) or []}
        grade = item.get("gradeInfo") or {}
        price_info = item.get("priceInfo") or {}

        # 价格字段可能显式返回 None，需兜底为 0
        selling_price = price_info.get("sellingPrice") or 0
        settle_price  = price_info.get("preSettlePrice") or 0

        # 提取购买渠道（国别/版本）
        channel = props.get("购买渠道") or "未知"

        return SoldRecord(
            model=clean_model_name(item.get("title") or ""),
            condition=build_condition(item.get("properties", []), grade),
            capacity=props.get("存储容量") or props.get("容量") or "未知",
            color=props.get("颜色") or "未知",
            list_time=list_time,
            sold_time=sold_time,
            sell_price=selling_price / 100,
            settle_price=settle_price / 100,
            channel=channel,
        )

    def _fetch_account(self, acc: Account, acc_idx: int, total: int) -> list[SoldRecord]:
        headers = {"cookie": acc.cookie, "user-agent": USER_AGENT}
        records: list[SoldRecord] = []
        now = pd.Timestamp.now()

        max_p = MAX_PAGES_ALL if self.mode == "all" else MAX_PAGES
        for page in range(1, max_p + 1):
            pct = (acc_idx / total + page / max_p / total) * 100
            self.on_progress(pct, f"正在拉取 {acc.name} 第 {page} 页...")

            try:
                resp = requests.post(
                    API_URL,
                    json={"query": {"pageNum": page, "pageSize": PAGE_SIZE, "statusList": ["80"]}},
                    headers=headers,
                    timeout=10,
                )
                resp.raise_for_status()
                body = resp.json()
            except requests.RequestException as e:
                self.on_log(f"⚠️  {acc.name} 第{page}页网络异常: {e}")
                break
            except ValueError:
                self.on_log(f"⚠️  {acc.name} 第{page}页响应解析失败")
                break

            items = (
                body.get("data", {}).get("list")
                or body.get("respData", {}).get("list")
                or []
            )
            if not items:
                break

            stop = False
            for item in items:
                record = self._parse_item(item)
                if record is None:
                    continue

                if self.mode == "days" and (now - record.sold_time).days > self.limit:
                    stop = True; break
                if self.mode == "count" and len(records) >= self.limit:
                    stop = True; break
                if self.since_date is not None and record.sold_time <= self.since_date:
                    stop = True; break

                records.append(record)

            if stop:
                break

        return records

    def run(self):
        all_records: list[SoldRecord] = []
        total = len(self.accounts)
        try:
            for idx, acc in enumerate(self.accounts):
                self.on_log(f"连接店铺: {acc.name}")
                acc_records = self._fetch_account(acc, idx, total)
                self.on_log(f"✅ {acc.name} 获取 {len(acc_records)} 条")
                all_records.extend(acc_records)
        except Exception as e:
            logger.exception("拉取数据时发生未知错误")
            self.on_error(str(e))
            return

        self.on_done(all_records)


# ─── 定价引擎 ────────────────────────────────────────────────────────────────
#
# 可调参数（改这里即可，无需动逻辑）：
ENGINE_WINDOW   = 60   # 参考窗口（天）：60天内成交数据参与计算，更早的丢弃
ENGINE_NEAR     = 30   # 近期分界（天）：近30天权重更高
ENGINE_W_RECENT = 3    # 近30天权重倍数（×3）
ENGINE_W_OLD    = 1    # 30-60天权重倍数（×1）
ENGINE_MIN_SAMPLE = 5  # 低置信度阈值：样本数低于此值时 UI 提示人工复核


@dataclass
class PricingResult:
    suggested_price: int    # 建议售价（已取整到8结尾）
    settle_price:    int    # 对应到手价（改价后从转转实时取，改价前用历史中位数）
    market_median:   int    # 加权市场中位价（参考）
    sample_count:    int    # 原始样本条数（未展开权重前）
    avg_sale_hours:  float  # 加权平均动销时长（小时）
    is_fast:         bool   # True=有24h内快速成交样本，False=保守价
    low_confidence:  bool   # True=样本不足或颜色无匹配，建议人工复核
    floor_price:     int    # 底价预警（加权P20），低于此基本亏损


def _estimate_settle(suggested_price: float, cur_settle: float, cur_price: float) -> int:
    """
    估算改价后的到手价。
    优先用当前 settle/price 比例推算实际扣点，无数据时用平台费率常量。
    """
    if cur_settle and cur_price:
        fee_rate = 1 - (cur_settle / cur_price)
    else:
        fee_rate = PLATFORM_FEE_RATE
    return int(round(suggested_price * (1 - fee_rate) - STATION_SERVICE_FEE))


def _round_to_8(price: int) -> int:
    """将价格调整为最近的以8结尾的整数（向下取）"""
    remainder = price % 10
    return price - remainder + 8 if remainder >= 8 else price - remainder - 2


def _wpct(series: "pd.Series", pct: float) -> int:
    """安全取分位数，返回整数；空时返回0"""
    if series.empty:
        return 0
    return int(series.quantile(pct / 100))


class PricingEngine:
    """
    定价引擎 v3（纯转转数据）

    筛选维度：型号 + 成色 + 容量 + 颜色（严格四维匹配）
              颜色无匹配时自动降级到三维，标记低置信度

    时间权重：
      近 ENGINE_NEAR(30) 天     × ENGINE_W_RECENT(3)
      30~ENGINE_WINDOW(60) 天   × ENGINE_W_OLD(1)
      60 天以外数据不参与

    价格逻辑：
      有 24h 内快速成交样本 → 极速动销价 = 快速成交加权中位数
      无 24h 快速成交样本   → 保守动销价 = 全量加权 P40
      底价预警              = 全量加权 P20（显示给用户参考）
    """

    def __init__(self, df: "pd.DataFrame"):
        self.df = df

    def query(self, model: str, condition: str,
              capacity: str, color: str, channel: str = "未知") -> "Optional[PricingResult]":

        now    = pd.Timestamp.now()
        cutoff = now - pd.Timedelta(days=ENGINE_WINDOW)

        def _filter(df, with_color: bool, with_channel: bool) -> "pd.DataFrame":
            mask = (
                (df["型号"]    == model)
                & (df["精确成色"] == condition)
                & (df["容量"]    == capacity)
            )
            if with_color:
                mask &= (df["颜色"] == color)
            if with_channel and "购买渠道" in df.columns:
                mask &= (df["购买渠道"] == channel)
            if "售出时间" in df.columns:
                mask &= (df["售出时间"] >= cutoff)
            return df[mask].copy()

        # 尝试五维匹配（型号+成色+容量+颜色+渠道）
        sub = _filter(self.df, with_color=True, with_channel=True)
        color_fallback = False
        channel_fallback = False

        if sub.empty:
            # 降级到四维（型号+成色+容量+颜色）
            sub = _filter(self.df, with_color=True, with_channel=False)
            channel_fallback = True

        if sub.empty:
            # 降级到三维（型号+成色+容量）
            sub = _filter(self.df, with_color=False, with_channel=False)
            color_fallback = True
            channel_fallback = True

        if sub.empty:
            return None

        # ── 时间权重展开 ───────────────────────────────────────────────────────
        age_days = (now - sub["售出时间"]).dt.total_seconds() / 86400
        sub = sub.copy()
        sub["_w"] = age_days.apply(
            lambda d: ENGINE_W_RECENT if d <= ENGINE_NEAR else ENGINE_W_OLD
        ).astype(int)

        # ── 剔除异常数据：过滤偏离中位数±10%以上的价格 ─────────────────────
        median_price = sub["最终售价"].median()
        if median_price > 0:
            lower_bound = median_price * 0.9
            upper_bound = median_price * 1.1
            sub = sub[(sub["最终售价"] >= lower_bound) & (sub["最终售价"] <= upper_bound)]

        if sub.empty:
            return None

        weighted = sub.loc[sub.index.repeat(sub["_w"])].copy()

        market_med  = int(weighted["最终售价"].median())
        floor_price = _wpct(weighted["最终售价"], 20)
        low_conf    = (len(sub) < ENGINE_MIN_SAMPLE) or color_fallback or channel_fallback

        # ── 极速动销价：24h内快速成交的加权中位数 ────────────────────────────
        fast_w = (
            weighted[weighted["sales_hours"] <= FAST_SALE_HOURS]
            if "sales_hours" in weighted.columns else pd.DataFrame()
        )

        if not fast_w.empty:
            price   = int(fast_w["最终售价"].median())
            settle  = _wpct(fast_w["预计最低结算价"], 50) if "预计最低结算价" in fast_w.columns else 0
            is_fast = True
        else:
            # 无快速成交记录 → 保守价取全量加权 P40
            price   = _wpct(weighted["最终售价"], 40)
            settle  = _wpct(weighted["预计最低结算价"], 40) if "预计最低结算价" in weighted.columns else 0
            is_fast = False

        avg_h = round(weighted["sales_hours"].median(), 1) if "sales_hours" in weighted.columns else 0.0

        return PricingResult(
            suggested_price = _round_to_8(price),
            settle_price    = settle,
            market_median   = market_med,
            sample_count    = len(sub),
            avg_sale_hours  = avg_h,
            is_fast         = is_fast,
            low_confidence  = low_conf,
            floor_price     = _round_to_8(floor_price),
        )

    @staticmethod
    def build_dataframe(records: list) -> "pd.DataFrame":
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame([asdict(r) for r in records])
        df.rename(columns={
            "model": "型号", "condition": "精确成色", "capacity": "容量", "color": "颜色",
            "channel": "购买渠道",
            "list_time": "上架时间", "sold_time": "售出时间",
            "sell_price": "最终售价", "settle_price": "预计最低结算价",
        }, inplace=True)
        df["上架时间"] = pd.to_datetime(df["上架时间"])
        df["售出时间"] = pd.to_datetime(df["售出时间"])
        df["sales_hours"] = (df["售出时间"] - df["上架时间"]).dt.total_seconds() / 3600
        return df[df["sales_hours"] >= 0].reset_index(drop=True)

# ─── 主 UI ──────────────────────────────────────────────────────────────────
class PricingAssistantApp:
    FONT_DEFAULT = ("PingFang SC", 13)
    FONT_BOLD = ("PingFang SC", 15, "bold")
    FONT_MONO = ("Menlo", 11)

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("转转多店极速调价中枢 v1.0")
        self.root.tk.call("tk", "scaling", 2.0)
        self.root.geometry("900x900")
        self.root.attributes("-topmost", True)

        self.account_mgr    = AccountManager()
        self.erp_config     = ErpConfig()            # 爱管机 ERP token
        self.cost_price_map = CostPriceMap()         # ERP成本底价表（本地缓存）
        self.engine: Optional[PricingEngine] = None
        self._imei_product: Optional[ProductDetail] = None
        self._data_store  = DataStore()
        self._batch_items: list[BatchItem] = []          # 全局商品列表，跨标签页共享
        self._inventory_win: Optional[tk.Toplevel] = None  # 商品状态独立窗口
        self._batch_stop_flag = False                      # 批量改价中止标志
        self._imp_refresh_running = False                  # 商品管理窗口定时刷新开关
        self._log_win = None                               # 自动化日志浮窗
        self._data_lab: Optional[DataLabWindow] = None    # 数据实验室浮窗

        # ── 企业微信自建应用 ──────────────────────────────────────────────────
        self.wxapp_config     = WxAppConfig()
        self.wxapp_client     = WxAppClient(self.wxapp_config)
        self.wxapp_dispatcher = WxAppCommandDispatcher()
        self._wxapp_server: Optional[HTTPServer] = None
        self._wxapp_server_thread: Optional[threading.Thread] = None
        self._wxapp_running   = False

        # ── 自动化状态 ────────────────────────────────────────────────────────
        self._auto_running = False          # 调度器总开关
        self._auto_thread: Optional[threading.Thread] = None

        self._build_ui()

        # 启动时自动加载本地缓存（如有）
        if self._data_store.latest_date() is not None:
            self.root.after(300, self._load_cache_into_engine)

        # 注册企业微信指令
        self._register_wxapp_commands()

    # ── UI 构建 ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        style = ttk.Style()
        style.configure("TProgressbar", thickness=20)

        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)
        self.notebook = nb

        self._build_tab_fetch(nb)
        self._build_tab_calc(nb)
        self._build_tab_imei(nb)
        self._build_tab_batch(nb)
        self._build_tab_auto(nb)
        self._build_tab_smart_reprice(nb)
        self._build_tab_ai_assistant(nb)
        self._build_tab_price_monitor(nb)

    def _build_tab_fetch(self, nb: ttk.Notebook):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" ⚙️ 数据管理 ")

        # 账号列表
        frm_acc = tk.LabelFrame(tab, text=" 店铺账号列表 ", font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=10)
        frm_acc.pack(fill=tk.X, padx=10, pady=10)

        self.tree_acc = ttk.Treeview(frm_acc, columns=("name", "status"), show="headings", height=3)
        self.tree_acc.heading("name", text="店铺名称")
        self.tree_acc.heading("status", text="状态")
        self.tree_acc.column("name", width=320)
        self.tree_acc.column("status", width=80, anchor="center")
        self.tree_acc.pack(fill=tk.X, pady=5)

        frm_input = tk.Frame(frm_acc)
        frm_input.pack(fill=tk.X, pady=5)
        for col, text in enumerate(["店名:", "Cookie:"]):
            tk.Label(frm_input, text=text, font=self.FONT_DEFAULT).grid(row=0, column=col * 2, sticky=tk.W)
        self.ent_acc_name = tk.Entry(frm_input, width=12, font=self.FONT_DEFAULT)
        self.ent_acc_name.grid(row=0, column=1, padx=5)
        self.ent_acc_cookie = tk.Entry(frm_input, width=28, font=self.FONT_DEFAULT)
        self.ent_acc_cookie.grid(row=0, column=3, padx=5)

        frm_btn = tk.Frame(frm_acc)
        frm_btn.pack(fill=tk.X, pady=5)
        ttk.Button(frm_btn, text="➕ 添加账号", command=self._add_account).pack(side=tk.LEFT, padx=5)
        ttk.Button(frm_btn, text="❌ 删除选中", command=self._delete_account).pack(side=tk.LEFT, padx=5)
        tk.Button(frm_btn, text="🔍 检测 Cookie", font=self.FONT_DEFAULT,
                  bg="#faad14", fg="black",
                  command=self._check_all_cookies).pack(side=tk.LEFT, padx=5)
        tk.Label(frm_btn, text="双击账号行可更新 Cookie",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=10)

        self.tree_acc.bind("<Double-1>", self._on_acc_double_click)
        self._refresh_account_list()

        # 任务配置 + 进度
        frm_task = tk.LabelFrame(tab, text=" 执行任务 ", font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=10)
        frm_task.pack(fill=tk.X, padx=10, pady=10)

        self.fetch_mode = tk.StringVar(value="days")
        frm_mode = tk.Frame(frm_task)
        frm_mode.pack(fill=tk.X, pady=5)
        tk.Radiobutton(frm_mode, text="按天数:", variable=self.fetch_mode, value="days",
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.ent_days = tk.Entry(frm_mode, width=5, font=self.FONT_DEFAULT)
        self.ent_days.insert(0, "30")
        self.ent_days.pack(side=tk.LEFT)
        tk.Radiobutton(frm_mode, text="按条数:", variable=self.fetch_mode, value="count",
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16, 0))
        self.ent_count = tk.Entry(frm_mode, width=5, font=self.FONT_DEFAULT)
        self.ent_count.insert(0, "200")
        self.ent_count.pack(side=tk.LEFT)
        tk.Radiobutton(frm_mode, text="全量数据", variable=self.fetch_mode, value="all",
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16, 0))
        tk.Label(frm_mode, text="（建议30天，近期数据权重更高）",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=8)

        self.progress_var = tk.DoubleVar()
        ttk.Progressbar(frm_task, variable=self.progress_var, maximum=100, mode="determinate").pack(fill=tk.X, pady=8)
        self.lbl_progress = tk.Label(frm_task, text="等待开始...", font=self.FONT_DEFAULT, fg="#8c8c8c")
        self.lbl_progress.pack()

        self.btn_fetch = tk.Button(
            tab, text="⚡️ 开始同步多店数据", command=self._start_fetch,
            font=("PingFang SC", 16, "bold"), bg="#1890ff", fg="black", height=2,
        )
        self.btn_fetch.pack(fill=tk.X, padx=20, pady=10)

        self.txt_log = tk.Text(tab, height=6, bg="#2b2b2b", fg="#a9b7c6", font=self.FONT_MONO, padx=10, pady=10)
        self.txt_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

    def _build_tab_calc(self, nb: ttk.Notebook):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 💰 调价工作台 ")
        self.tab_calc = tab

        self.lbl_calc_status = tk.Label(tab, text="🔴 等待数据接入...", font=self.FONT_BOLD, fg="#cf1322", pady=10)
        self.lbl_calc_status.pack()

        frm_sel = tk.Frame(tab)
        frm_sel.pack(fill=tk.X, padx=30, pady=5)

        # 搜索框
        tk.Label(frm_sel, text="🔍 型号过滤:", font=self.FONT_DEFAULT, fg="#1976d2").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.ent_search = tk.Entry(frm_sel, font=self.FONT_DEFAULT, width=35)
        self.ent_search.grid(row=0, column=1, padx=15, pady=5)
        self.ent_search.bind("<KeyRelease>", self._filter_models)

        # 联动下拉框
        self.combos: dict[str, ttk.Combobox] = {}
        combo_cfg = [
            ("型号:", "model", self._update_conditions),
            ("成色:", "condition", self._update_capacities),
            ("容量:", "capacity", self._update_colors),
            ("颜色:", "color", self._update_channels),
            ("购买渠道:", "channel", self._calculate_price),
        ]
        for row, (label, key, cb_func) in enumerate(combo_cfg, start=1):
            tk.Label(frm_sel, text=label, font=self.FONT_DEFAULT).grid(row=row, column=0, sticky=tk.W, pady=10)
            cb = ttk.Combobox(frm_sel, state="readonly", font=self.FONT_DEFAULT, width=37)
            cb.grid(row=row, column=1, padx=15, pady=10)
            cb.bind("<<ComboboxSelected>>", cb_func)
            self.combos[key] = cb

        # 结果区
        frm_res = tk.LabelFrame(tab, text=" 调价建议 ", font=self.FONT_BOLD, fg="#1976d2", padx=20, pady=20)
        frm_res.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        self.lbl_suggested = tk.Label(frm_res, text="建议卖价: -- 元", font=("PingFang SC", 22, "bold"), fg="#a8071a")
        self.lbl_suggested.pack(pady=10)
        self.lbl_settlement = tk.Label(frm_res, text="预计到手: -- 元", font=("PingFang SC", 18, "bold"), fg="#237804")
        self.lbl_settlement.pack(pady=5)
        self.lbl_market = tk.Label(frm_res, text="市场中位价: -- 元", font=self.FONT_DEFAULT)
        self.lbl_market.pack()
        self.lbl_speed = tk.Label(frm_res, text="平均售出时长: -- 小时", font=self.FONT_DEFAULT, fg="#8c8c8c")
        self.lbl_speed.pack(pady=5)
        self.lbl_floor = tk.Label(frm_res, text="", font=self.FONT_DEFAULT, fg="#cf1322")
        self.lbl_floor.pack()
        self.lbl_confidence = tk.Label(frm_res, text="", font=("PingFang SC", 12), fg="#fa8c16")
        self.lbl_confidence.pack(pady=2)
        self.btn_weight_detail = tk.Button(
            frm_res, text="📊 查看权重明细", font=("PingFang SC", 12),
            bg="#f0f0f0", relief="flat", state="disabled",
            command=self._open_weight_detail
        )
        self.btn_weight_detail.pack(pady=(6, 0))
        self._last_weight_query = None   # 缓存最近一次查询参数，供明细窗口用

    # ── 账号操作 ─────────────────────────────────────────────────────────────
    def _refresh_account_list(self, statuses: dict[str, tuple[bool, str]] | None = None):
        for row in self.tree_acc.get_children():
            self.tree_acc.delete(row)
        for acc in self.account_mgr.accounts:
            if statuses and acc.name in statuses:
                ok, reason = statuses[acc.name]
                tag    = "ok" if ok else "fail"
                status = "✅ 有效" if ok else f"❌ 失效"
            else:
                tag    = "unknown"
                status = "⬜ 未检测"
            self.tree_acc.insert("", tk.END, iid=acc.name,
                                 values=(acc.name, status), tags=(tag,))
        self.tree_acc.tag_configure("ok",      foreground="#389e0d")
        self.tree_acc.tag_configure("fail",    foreground="#cf1322")
        self.tree_acc.tag_configure("unknown", foreground="#888888")

    def _check_all_cookies(self):
        """后台逐个检测所有账号 cookie，完成后刷新列表，失效账号弹窗提示"""
        def _worker():
            statuses: dict[str, tuple[bool, str]] = {}
            for acc in self.account_mgr.accounts:
                ok, reason = ImeiService(acc.cookie).check_cookie_valid()
                statuses[acc.name] = (ok, reason)
                tag    = "✅ 有效" if ok else "❌ 失效"
                self.root.after(0, lambda n=acc.name, s=tag, t=("ok" if ok else "fail"): (
                    self.tree_acc.set(n, "status", s),
                    self.tree_acc.item(n, tags=(t,)),
                ))
            # 检测完成后，对失效的逐一弹窗
            invalid = [n for n, (ok, _) in statuses.items() if not ok]
            if invalid:
                self.root.after(0, lambda: self._prompt_update_cookies(invalid))
            else:
                self.root.after(0, lambda: messagebox.showinfo("检测完成", "所有账号 Cookie 均有效 ✅"))

        threading.Thread(target=_worker, daemon=True).start()

    def _prompt_update_cookies(self, invalid_names: list[str]):
        """对失效账号逐一弹窗，让用户粘贴新 cookie"""
        for name in invalid_names:
            self._show_cookie_update_dialog(name)

    def _show_cookie_update_dialog(self, name: str):
        """弹出 cookie 更新对话框"""
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Cookie 已失效 — {name}")
        dlg.geometry("620x220")
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text=f"账号「{name}」的 Cookie 已失效，请重新粘贴新的 Cookie：",
                 font=self.FONT_DEFAULT, wraplength=580).pack(padx=16, pady=(16, 6), anchor="w")

        txt = tk.Text(dlg, height=4, font=("Menlo", 11),
                      bg="#fafafa", relief="solid", bd=1, padx=6, pady=4)
        txt.pack(fill=tk.X, padx=16, pady=4)

        # 预填旧 cookie 方便对比
        for acc in self.account_mgr.accounts:
            if acc.name == name:
                txt.insert("1.0", acc.cookie)
                txt.tag_add("sel", "1.0", tk.END)
                break

        frm_btn = tk.Frame(dlg); frm_btn.pack(pady=8)

        def _save():
            new_cookie = txt.get("1.0", tk.END).strip()
            if not new_cookie:
                messagebox.showwarning("提示", "Cookie 不能为空", parent=dlg)
                return
            for acc in self.account_mgr.accounts:
                if acc.name == name:
                    acc.cookie = new_cookie
                    break
            self.account_mgr.save()
            # 立即验证新 cookie
            def _verify():
                ok, reason = ImeiService(new_cookie).check_cookie_valid()
                if ok:
                    self.root.after(0, lambda: (
                        self.tree_acc.set(name, "status", "✅ 有效"),
                        self.tree_acc.item(name, tags=("ok",)),
                        messagebox.showinfo("成功", f"账号「{name}」Cookie 已更新且验证有效 ✅"),
                    ))
                else:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "警告", f"Cookie 已保存，但验证仍未通过：{reason}\n请确认粘贴是否完整。"))
                self._refresh_auto_account_list()
            threading.Thread(target=_verify, daemon=True).start()
            dlg.destroy()

        tk.Button(frm_btn, text="💾 保存并验证", font=self.FONT_DEFAULT,
                  bg="#1890ff", fg="black", command=_save).pack(side=tk.LEFT, padx=8)
        tk.Button(frm_btn, text="跳过", font=self.FONT_DEFAULT,
                  command=dlg.destroy).pack(side=tk.LEFT, padx=8)

    def _add_account(self):
        name = self.ent_acc_name.get().strip()
        cookie = self.ent_acc_cookie.get().strip()
        try:
            self.account_mgr.add(name, cookie)
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self._refresh_account_list()
        self.ent_acc_name.delete(0, tk.END)
        self.ent_acc_cookie.delete(0, tk.END)

    def _delete_account(self):
        sel = self.tree_acc.selection()
        if not sel:
            return
        name = self.tree_acc.item(sel[0], "values")[0]
        self.account_mgr.remove(name)
        self._refresh_account_list()

    def _on_acc_double_click(self, event):
        sel = self.tree_acc.selection()
        if not sel:
            return
        name = self.tree_acc.item(sel[0], "values")[0]
        self._show_cookie_update_dialog(name)

    # ── 数据拉取 ─────────────────────────────────────────────────────────────
    def _start_fetch(self):
        if not self.account_mgr.accounts:
            messagebox.showwarning("提示", "请先添加账号")
            return
        mode = self.fetch_mode.get()
        try:
            if mode == "days":
                limit = int(self.ent_days.get())
            elif mode == "count":
                limit = int(self.ent_count.get())
            else:  # "all"
                limit = 99999
        except ValueError:
            messagebox.showwarning("提示", "请输入有效的数字")
            return

        # 检查是否有缓存，给用户选择全量还是增量
        since_date = None
        latest = self._data_store.latest_date()
        if latest is not None:
            date_str = latest.strftime("%Y-%m-%d %H:%M")
            choice = messagebox.askyesnocancel(
                "检测到本地缓存",
                f"本地已有缓存数据，最新记录日期：{date_str}\n\n"
                f"• 点「是」→ 增量同步（只拉取 {date_str} 之后的新数据）\n"
                f"• 点「否」→ 全量重新拉取（覆盖缓存）\n"
                f"• 点「取消」→ 不拉取，直接加载缓存",
            )
            if choice is None:          # 取消 → 加载缓存直接用
                self._load_cache_into_engine()
                return
            elif choice:                # 是 → 增量
                since_date = latest

        self.btn_fetch.config(state="disabled", text="⚡️ 正在同步数据...")
        self.progress_var.set(0)

        fetcher = DataFetcher(
            accounts=self.account_mgr.accounts,
            mode=self.fetch_mode.get(),
            limit=limit,
            on_progress=self._on_progress,
            on_log=self._log,
            on_done=self._on_fetch_done,
            on_error=self._on_fetch_error,
            since_date=since_date,
        )
        threading.Thread(target=fetcher.run, daemon=True).start()

    def _on_progress(self, pct: float, msg: str):
        self.root.after(0, lambda: (
            self.progress_var.set(pct),
            self.lbl_progress.config(text=msg),
        ))

    def _load_cache_into_engine(self):
        """直接加载本地缓存进引擎，无需网络请求"""
        df = self._data_store.load()
        if df.empty:
            messagebox.showinfo("提示", "本地缓存为空，请先执行同步")
            return
        if "sales_hours" not in df.columns:
            df["sales_hours"] = (
                (df["售出时间"] - df["上架时间"]).dt.total_seconds() / 3600
            )
        df = df[df["sales_hours"] >= 0].reset_index(drop=True)
        self.engine = PricingEngine(df)
        latest = df["售出时间"].max()
        self.lbl_calc_status.config(
            text=f"🟢 缓存已加载（{len(df)} 条，最新 {pd.Timestamp(latest).strftime('%m-%d')}）",
            fg="#389e0d",
        )
        self.lbl_progress.config(text=f"📦 已从缓存加载 {len(df)} 条数据")
        models = df["型号"].unique().tolist()
        self.combos["model"]["values"] = models
        if models:
            self.combos["model"].set(models[0])
            self._update_conditions()
        self.notebook.select(self.tab_calc)

    def _on_fetch_done(self, records: list[SoldRecord]):
        def _update():
            self.btn_fetch.config(state="normal", text="⚡️ 开始同步多店数据")
            self.progress_var.set(100)

            if not records:
                self._log("本次未抓取到新数据，尝试加载缓存...")
                self._load_cache_into_engine()
                self.lbl_progress.config(text="✅ 无新数据，已加载缓存")
                return

            # 构建新数据 DataFrame
            new_df = PricingEngine.build_dataframe(records)

            # 与缓存合并
            old_df = self._data_store.load()
            merged = DataStore.merge(old_df, new_df)
            self._data_store.save(merged)

            self.engine = PricingEngine(merged)
            new_count = len(new_df)
            total_count = len(merged)
            latest = merged["售出时间"].max()
            self.lbl_progress.config(
                text=f"🎉 同步完成！新增 {new_count} 条，共 {total_count} 条"
            )
            self.lbl_calc_status.config(
                text=(f"🟢 数据已就绪（{total_count} 条，"
                      f"最新 {pd.Timestamp(latest).strftime('%m-%d')}）"),
                fg="#389e0d",
            )

            models = merged["型号"].unique().tolist()
            self.combos["model"]["values"] = models
            if models:
                self.combos["model"].set(models[0])
                self._update_conditions()

            self.notebook.select(self.tab_calc)

        self.root.after(0, _update)

    def _on_fetch_error(self, msg: str):
        self.root.after(0, lambda: (
            self.btn_fetch.config(state="normal", text="⚡️ 开始同步多店数据"),
            messagebox.showerror("拉取失败", f"发生错误：{msg}"),
        ))

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: (
            self.txt_log.insert(tk.END, f"[{ts}] {msg}\n"),
            self.txt_log.see(tk.END),
        ))

    # ── 调价逻辑 ─────────────────────────────────────────────────────────────
    def _filter_models(self, _event=None):
        if self.engine is None:
            return
        kw = self.ent_search.get().strip().lower()
        all_models = self.engine.df["型号"].unique().tolist()
        filtered = [m for m in all_models if kw in m.lower()] if kw else all_models
        self.combos["model"]["values"] = filtered
        if len(filtered) == 1:
            self.combos["model"].set(filtered[0])
            self._update_conditions()
        elif not filtered:
            self.combos["model"].set("无匹配结果")

    def _get_filtered_df(self, *keys: str) -> pd.DataFrame:
        """按已选下拉值逐级过滤 DataFrame"""
        col_map = {"model": "型号", "condition": "精确成色", "capacity": "容量", "color": "颜色", "channel": "购买渠道"}
        df = self.engine.df
        for key in keys:
            df = df[df[col_map[key]] == self.combos[key].get()]
        return df

    def _update_conditions(self, _e=None):
        if self.engine is None:
            return
        self._cascade_combo("精确成色", self._get_filtered_df("model"), self._update_capacities)

    def _update_capacities(self, _e=None):
        if self.engine is None:
            return
        self._cascade_combo("容量", self._get_filtered_df("model", "condition"), self._update_colors)

    def _update_colors(self, _e=None):
        if self.engine is None:
            return
        self._cascade_combo("颜色", self._get_filtered_df("model", "condition", "capacity"), self._update_channels)

    def _update_channels(self, _e=None):
        if self.engine is None:
            return
        self._cascade_combo("购买渠道", self._get_filtered_df("model", "condition", "capacity", "color"), self._calculate_price)

    def _calculate_price(self, _e=None):
        if self.engine is None:
            return
        result = self.engine.query(
            model=self.combos["model"].get(),
            condition=self.combos["condition"].get(),
            capacity=self.combos["capacity"].get(),
            color=self.combos["color"].get(),
            channel=self.combos["channel"].get(),
        )
        if result is None:
            return

        if result.is_fast:
            self.lbl_suggested.config(
                text=f"🔥 极速动销价: {result.suggested_price} 元", fg="#d32f2f"
            )
        else:
            self.lbl_suggested.config(
                text=f"🐢 保守动销价: {result.suggested_price} 元", fg="#f57c00"
            )
        self.lbl_settlement.config(text=f"💸 预计到手: {result.settle_price} 元")
        self.lbl_market.config(text=f"市场中位价: {result.market_median} 元（参考 {result.sample_count} 条成交）")
        self.lbl_speed.config(text=f"平均动销时长: {result.avg_sale_hours} 小时")
        self.lbl_floor.config(
            text=f"⚠️ 底价预警: {result.floor_price} 元（低于此价亏损风险高）" if result.floor_price else ""
        )
        self.lbl_confidence.config(
            text="⚠️ 样本不足或颜色无精确匹配，建议人工核对" if result.low_confidence else ""
        )
        # 缓存本次查询参数，激活权重明细按钮
        self._last_weight_query = (
            self.combos["model"].get(),
            self.combos["condition"].get(),
            self.combos["capacity"].get(),
            self.combos["color"].get(),
            self.combos["channel"].get(),
        )
        self.btn_weight_detail.config(state="normal")

    def _open_weight_detail(self):
        """弹窗：展示当前型号参与定价计算的每条成交记录及其时间权重"""
        if not self._last_weight_query or self.engine is None:
            return
        model, condition, capacity, color = self._last_weight_query

        now    = pd.Timestamp.now()
        cutoff = now - pd.Timedelta(days=ENGINE_WINDOW)

        def _filter(with_color):
            mask = (
                (self.engine.df["型号"]    == model)
                & (self.engine.df["精确成色"] == condition)
                & (self.engine.df["容量"]    == capacity)
            )
            if with_color:
                mask &= (self.engine.df["颜色"] == color)
            if "售出时间" in self.engine.df.columns:
                mask &= (self.engine.df["售出时间"] >= cutoff)
            return self.engine.df[mask].copy()

        sub = _filter(True)
        color_fallback = sub.empty
        if color_fallback:
            sub = _filter(False)

        if sub.empty:
            messagebox.showinfo("无数据", "该规格在60天内无成交记录")
            return

        # 计算权重和分类
        age_days = (now - sub["售出时间"]).dt.total_seconds() / 86400
        sub = sub.copy()
        sub["_age_days"] = age_days.round(1)
        sub["_weight"]   = age_days.apply(
            lambda d: ENGINE_W_RECENT if d <= ENGINE_NEAR else ENGINE_W_OLD
        ).astype(int)
        sub["_is_fast"]  = (sub["sales_hours"] <= FAST_SALE_HOURS) if "sales_hours" in sub.columns else False
        sub = sub.sort_values("售出时间", ascending=False)

        # ── 弹窗 ──────────────────────────────────────────────────────────────
        win = tk.Toplevel(self.root)
        title_color = "颜色降级（无精确颜色匹配）" if color_fallback else color
        win.title(f"权重明细 — {model} {condition} {capacity} {title_color}")
        win.geometry("780x520")
        win.lift()

        # 汇总行
        near_n = int((sub["_weight"] == ENGINE_W_RECENT).sum())
        old_n  = int((sub["_weight"] == ENGINE_W_OLD).sum())
        fast_n = int(sub["_is_fast"].sum())
        total_w = near_n * ENGINE_W_RECENT + old_n * ENGINE_W_OLD

        summary = (
            f"共 {len(sub)} 条原始记录（近{ENGINE_NEAR}天 {near_n}条×{ENGINE_W_RECENT} + "
            f"{ENGINE_NEAR}-{ENGINE_WINDOW}天 {old_n}条×{ENGINE_W_OLD}）"
            f"  展开后 {total_w} 个加权样本  24h极速成交 {fast_n} 条"
        )
        if color_fallback:
            summary += "  ⚠️ 颜色无精确匹配，已降级忽略颜色"
        tk.Label(win, text=summary, font=("PingFang SC", 11),
                 fg="#555", wraplength=750, justify="left").pack(padx=10, pady=(8, 4), anchor="w")

        # 表格
        cols = ("sold_time", "age_days", "weight", "sell_price", "sales_hours", "is_fast")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=18)
        tree.heading("sold_time",   text="成交时间")
        tree.heading("age_days",    text="距今天数")
        tree.heading("weight",      text="权重")
        tree.heading("sell_price",  text="成交价")
        tree.heading("sales_hours", text="动销时长(h)")
        tree.heading("is_fast",     text="极速?")
        tree.column("sold_time",   width=140, anchor="center")
        tree.column("age_days",    width=80,  anchor="center")
        tree.column("weight",      width=70,  anchor="center")
        tree.column("sell_price",  width=90,  anchor="center")
        tree.column("sales_hours", width=100, anchor="center")
        tree.column("is_fast",     width=65,  anchor="center")

        tree.tag_configure("recent", background="#eff6ff")   # 近30天淡蓝
        tree.tag_configure("fast",   background="#f0fae8")   # 极速成交淡绿

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=6)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, pady=6, padx=(0, 6))

        for _, row in sub.iterrows():
            sold_str  = row["售出时间"].strftime("%Y-%m-%d %H:%M") if pd.notna(row["售出时间"]) else "--"
            age_str   = f"{row['_age_days']:.0f}天"
            w_str     = f"×{int(row['_weight'])}  {'(近期)' if row['_weight'] == ENGINE_W_RECENT else '(较早)'}"
            price_str = f"¥{int(row['最终售价'])}"
            hours_str = f"{row['sales_hours']:.1f}h" if "sales_hours" in sub.columns else "--"
            fast_str  = "🔥是" if row["_is_fast"] else "否"
            tag = "fast" if row["_is_fast"] else ("recent" if row["_weight"] == ENGINE_W_RECENT else "")
            tree.insert("", tk.END, values=(sold_str, age_str, w_str, price_str, hours_str, fast_str), tags=(tag,))

    def _cascade_combo(self, col: str, df: pd.DataFrame, next_fn=None):
        """通用联动：按 df 列名取唯一值 → 填对应 combo → 触发下一级"""
        key_map = {"精确成色": "condition", "容量": "capacity", "颜色": "color", "购买渠道": "channel"}
        combo_key = key_map.get(col, col)
        values = df[col].value_counts().index.tolist()
        self.combos[combo_key]["values"] = values
        if values:
            self.combos[combo_key].set(values[0])
            if next_fn:
                next_fn()


    # ── IMEI / 质检码标签页 ───────────────────────────────────────────────────
    def _build_tab_imei(self, nb: ttk.Notebook):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 📱 质检码定价/改价 ")
        self.tab_imei = tab

        # ── 顶部：账号选择 + 编码输入 ────────────────────────────────────────
        frm_input = tk.LabelFrame(
            tab, text=" 第一步：输入编码 ", font=self.FONT_BOLD, fg="#1976d2", padx=12, pady=12
        )
        frm_input.pack(fill=tk.X, padx=15, pady=(15, 8))

        # 账号选择行
        row0 = tk.Frame(frm_input)
        row0.pack(fill=tk.X, pady=4)
        tk.Label(row0, text="操作店铺:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.combo_imei_account = ttk.Combobox(row0, state="readonly", font=self.FONT_DEFAULT, width=20)
        self.combo_imei_account.pack(side=tk.LEFT, padx=10)
        ttk.Button(row0, text="🔄 刷新", command=self._refresh_imei_account_list).pack(side=tk.LEFT)
        self._refresh_imei_account_list()

        # 编码输入行
        row1 = tk.Frame(frm_input)
        row1.pack(fill=tk.X, pady=8)
        tk.Label(row1, text="质检码:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.ent_imei = tk.Entry(row1, font=("PingFang SC", 14), width=24)
        self.ent_imei.pack(side=tk.LEFT, padx=10)
        self.ent_imei.bind("<Return>", lambda _e: self._start_imei_lookup())  # 回车触发
        self.btn_imei_search = ttk.Button(row1, text="🔍 查询", command=self._start_imei_lookup)
        self.btn_imei_search.pack(side=tk.LEFT)
        tk.Label(row1, text="（输入转转质检码，如：993801754）",
                 font=("PingFang SC", 11), fg="#aaaaaa").pack(side=tk.LEFT, padx=6)

        # ── 中部：商品信息面板 ───────────────────────────────────────────────
        frm_info = tk.LabelFrame(
            tab, text=" 第二步：商品信息 ", font=self.FONT_BOLD, fg="#1976d2", padx=12, pady=12
        )
        frm_info.pack(fill=tk.X, padx=15, pady=8)

        # 使用 grid 布局让字段对齐
        info_fields = ["标题", "型号", "成色", "容量", "颜色", "状态"]
        self._info_labels: dict[str, tk.Label] = {}
        for i, field_name in enumerate(info_fields):
            tk.Label(frm_info, text=f"{field_name}:", font=self.FONT_DEFAULT, width=6, anchor="e").grid(
                row=i // 2, column=(i % 2) * 2, sticky=tk.E, padx=(0, 4), pady=4
            )
            lbl = tk.Label(frm_info, text="--", font=self.FONT_DEFAULT, fg="#444", anchor="w", width=28)
            lbl.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky=tk.W, pady=4)
            self._info_labels[field_name] = lbl

        # 当前价格 + 预计结算（单独一行，更突出）
        frm_prices = tk.Frame(frm_info)
        frm_prices.grid(row=3, column=0, columnspan=4, pady=8, sticky=tk.W)
        tk.Label(frm_prices, text="当前售价:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.lbl_imei_cur_price = tk.Label(
            frm_prices, text="-- 元", font=("PingFang SC", 15, "bold"), fg="#555"
        )
        self.lbl_imei_cur_price.pack(side=tk.LEFT, padx=(4, 24))
        tk.Label(frm_prices, text="当前结算:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.lbl_imei_cur_settle = tk.Label(
            frm_prices, text="-- 元", font=("PingFang SC", 15, "bold"), fg="#555"
        )
        self.lbl_imei_cur_settle.pack(side=tk.LEFT, padx=4)

        # ── 中部：定价建议 ───────────────────────────────────────────────────
        frm_suggest = tk.LabelFrame(
            tab, text=" 第三步：定价建议（来自历史成交数据）", font=self.FONT_BOLD, fg="#1976d2", padx=12, pady=12
        )
        frm_suggest.pack(fill=tk.X, padx=15, pady=8)

        self.lbl_imei_suggest = tk.Label(
            frm_suggest, text="暂无建议（请先同步数据）",
            font=("PingFang SC", 18, "bold"), fg="#999"
        )
        self.lbl_imei_suggest.pack()
        self.lbl_imei_suggest_detail = tk.Label(
            frm_suggest, text="", font=self.FONT_DEFAULT, fg="#8c8c8c"
        )
        self.lbl_imei_suggest_detail.pack(pady=2)

        # ── 底部：改价操作 ───────────────────────────────────────────────────
        frm_action = tk.LabelFrame(
            tab, text=" 第四步：确认改价 ", font=self.FONT_BOLD, fg="#d32f2f", padx=12, pady=12
        )
        frm_action.pack(fill=tk.X, padx=15, pady=8)

        row_price = tk.Frame(frm_action)
        row_price.pack(fill=tk.X, pady=6)

        tk.Label(row_price, text="新售价（元）:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.ent_new_price = tk.Entry(row_price, font=("PingFang SC", 16, "bold"), width=10)
        self.ent_new_price.pack(side=tk.LEFT, padx=10)

        # 一键填入建议价按钮
        ttk.Button(
            row_price, text="← 填入建议价", command=self._fill_suggested_price
        ).pack(side=tk.LEFT, padx=5)

        self.lbl_new_settle_preview = tk.Label(
            row_price, text="", font=self.FONT_DEFAULT, fg="#2e7d32"
        )
        self.lbl_new_settle_preview.pack(side=tk.LEFT, padx=12)
        self.ent_new_price.bind("<KeyRelease>", self._preview_settle)

        self.btn_update_price = tk.Button(
            frm_action, text="✅ 确认改价",
            command=self._start_price_update,
            font=("PingFang SC", 15, "bold"),
            bg="#52c41a", fg="black", height=2, state="disabled",
        )
        self.btn_update_price.pack(fill=tk.X, pady=(8, 0))

        # 状态反馈
        self.lbl_imei_status = tk.Label(
            tab, text="请输入 IMEI 或质检码后点击查询", font=self.FONT_DEFAULT, fg="#8c8c8c", pady=6
        )
        self.lbl_imei_status.pack()

    # ── IMEI 相关辅助方法 ─────────────────────────────────────────────────────
    def _refresh_imei_account_list(self):
        names = [a.name for a in self.account_mgr.accounts]
        self.combo_imei_account["values"] = names
        if names and not self.combo_imei_account.get():
            self.combo_imei_account.set(names[0])

    def _get_selected_cookie(self) -> Optional[str]:
        name = self.combo_imei_account.get()
        for a in self.account_mgr.accounts:
            if a.name == name:
                return a.cookie
        return None

    def _fill_suggested_price(self):
        """将建议价填入改价输入框"""
        text = self.lbl_imei_suggest.cget("text")
        # 从标签文本里提取数字，例如 "🔥 极速动销价: 1299 元"
        m = re.search(r"(\d+)", text)
        if m:
            self.ent_new_price.delete(0, tk.END)
            self.ent_new_price.insert(0, m.group(1))
            self._preview_settle()

    def _preview_settle(self, _e=None):
        """实时预览新价格下的结算金额"""
        try:
            new_price = float(self.ent_new_price.get())
            est_settle = round(new_price * (1 - PLATFORM_FEE_RATE) - STATION_SERVICE_FEE, 2)
            self.lbl_new_settle_preview.config(
                text=f"≈ 预计到手 {est_settle} 元（扣{int(PLATFORM_FEE_RATE*100)}%−¥{STATION_SERVICE_FEE}服务费）",
                fg="#2e7d32",
            )
        except ValueError:
            self.lbl_new_settle_preview.config(text="")

    def _set_imei_status(self, msg: str, color: str = "#8c8c8c"):
        self.root.after(0, lambda: self.lbl_imei_status.config(text=msg, fg=color))

    def _reset_info_panel(self):
        for lbl in self._info_labels.values():
            lbl.config(text="--")
        self.lbl_imei_cur_price.config(text="-- 元")
        self.lbl_imei_cur_settle.config(text="-- 元")
        self.lbl_imei_suggest.config(text="暂无建议（请先同步数据）", fg="#999")
        self.lbl_imei_suggest_detail.config(text="")
        self.btn_update_price.config(state="disabled")
        self._imei_product = None

    def _populate_info_panel(self, p: ProductDetail):
        """把查询到的商品信息填入面板"""
        field_map = {
            "标题": p.title,
            "型号": p.model,
            "成色": p.condition,
            "容量": p.capacity,
            "颜色": p.color,
            "状态": p.status,
        }
        for field_name, value in field_map.items():
            self._info_labels[field_name].config(text=value or "--")
        self.lbl_imei_cur_price.config(text=f"{p.current_price:.0f} 元", fg="#333")
        self.lbl_imei_cur_settle.config(text=f"{p.settle_price:.0f} 元", fg="#333")
        self.btn_update_price.config(state="normal")

    def _populate_suggestion(self, p: ProductDetail):
        """根据商品属性从 PricingEngine 拉取建议价并填入"""
        if self.engine is None:
            self.lbl_imei_suggest.config(text="暂无建议（请先同步数据）", fg="#999")
            return
        result = self.engine.query(
            model=p.model,
            condition=p.condition,
            capacity=p.capacity,
            color=p.color,
        )
        if result is None:
            self.lbl_imei_suggest.config(text="历史数据中无匹配记录", fg="#999")
            return

        if result.is_fast:
            self.lbl_imei_suggest.config(
                text=f"🔥 极速动销价: {result.suggested_price} 元", fg="#d32f2f"
            )
        else:
            self.lbl_imei_suggest.config(
                text=f"🐢 保守动销价: {result.suggested_price} 元", fg="#f57c00"
            )
        self.lbl_imei_suggest_detail.config(
            text=(
                f"市场中位价 {result.market_median} 元 | "
                f"预计到手 {result.settle_price} 元 | "
                f"样本 {result.sample_count} 台 | "
                f"平均售出 {result.avg_sale_hours}h"
            )
        )

    # ── IMEI 查询（线程） ─────────────────────────────────────────────────────
    def _start_imei_lookup(self):
        code = self.ent_imei.get().strip()
        if not code:
            messagebox.showwarning("提示", "请输入 IMEI 或质检码")
            return
        cookie = self._get_selected_cookie()
        if not cookie:
            messagebox.showwarning("提示", "请先选择操作店铺")
            return

        self._reset_info_panel()
        self.btn_imei_search.config(state="disabled")
        self._set_imei_status("🔍 正在查询，请稍候...", "#1976d2")

        def _worker():
            try:
                product = ImeiService(cookie).lookup(code)
            except requests.RequestException as e:
                msg = f"❌ 网络异常：{e}"
                self.root.after(0, lambda m=msg: (
                    self._set_imei_status(m, "#cf1322"),
                    self.btn_imei_search.config(state="normal"),
                ))
                return
            except ValueError as e:
                msg = f"❌ 查询失败：{e}"
                self.root.after(0, lambda m=msg: (
                    self._set_imei_status(m, "#cf1322"),
                    self.btn_imei_search.config(state="normal"),
                ))
                return

            def _update():
                self._imei_product = product
                self._populate_info_panel(product)
                self._populate_suggestion(product)
                self.btn_imei_search.config(state="normal")
                self._set_imei_status("✅ 查询成功", "#389e0d")

            self.root.after(0, _update)

        threading.Thread(target=_worker, daemon=True).start()

    # ── 改价（线程） ──────────────────────────────────────────────────────────
    def _start_price_update(self):
        if self._imei_product is None:
            messagebox.showwarning("提示", "请先查询商品")
            return
        try:
            new_price = float(self.ent_new_price.get())
        except ValueError:
            messagebox.showwarning("提示", "请输入有效的新价格")
            return
        if new_price <= 0:
            messagebox.showwarning("提示", "价格必须大于 0")
            return

        cookie = self._get_selected_cookie()
        if not cookie:
            messagebox.showwarning("提示", "请先选择操作店铺")
            return

        confirm = messagebox.askyesno(
            "确认改价",
            f"即将把\n「{self._imei_product.title}」\n"
            f"售价从 {self._imei_product.current_price:.0f} 元 → {new_price:.0f} 元\n\n确认提交？",
        )
        if not confirm:
            return

        self.btn_update_price.config(state="disabled", text="⏳ 提交中...")
        self._set_imei_status("正在提交改价...", "#1976d2")

        product = self._imei_product   # 快照，避免线程中途被覆盖

        def _worker():
            try:
                msg = ImeiService(cookie).update_price(product, new_price)
            except requests.RequestException as e:
                err = f"❌ 网络异常：{e}"
                self.root.after(0, lambda m=err: (
                    self.btn_update_price.config(state="normal", text="✅ 确认改价"),
                    self._set_imei_status(m, "#cf1322"),
                ))
                return
            except ValueError as e:
                err = f"❌ 改价失败：{e}"
                self.root.after(0, lambda m=err: (
                    self.btn_update_price.config(state="normal", text="✅ 确认改价"),
                    self._set_imei_status(m, "#cf1322"),
                ))
                return

            def _ok():
                # 更新面板上的当前售价
                self._imei_product.current_price = new_price
                self.lbl_imei_cur_price.config(text=f"{new_price:.0f} 元", fg="#389e0d")
                self.btn_update_price.config(state="normal", text="✅ 确认改价")
                self._set_imei_status(f"🎉 改价成功！{msg}", "#389e0d")
                messagebox.showinfo("改价成功", f"{msg}\n新售价：{new_price:.0f} 元")

            self.root.after(0, _ok)

        threading.Thread(target=_worker, daemon=True).start()


    # ══════════════════════════════════════════════════════════════════════════
    # 批量调价标签页
    # ══════════════════════════════════════════════════════════════════════════
    def _build_tab_batch(self, nb: ttk.Notebook):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 🚀 批量调价 ")
        self.tab_batch = tab
        self._status_filters: dict[str, tk.BooleanVar] = {}

        # ── 控制栏 ───────────────────────────────────────────────────────────
        frm_ctrl = tk.LabelFrame(
            tab, text=" 操作 ", font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=8
        )
        frm_ctrl.pack(fill=tk.X, padx=12, pady=(12, 4))

        row_top = tk.Frame(frm_ctrl)
        row_top.pack(fill=tk.X, pady=4)

        tk.Label(row_top, text="操作店铺:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.combo_batch_account = ttk.Combobox(
            row_top, state="readonly", font=self.FONT_DEFAULT, width=18
        )
        self.combo_batch_account.pack(side=tk.LEFT, padx=(6, 16))
        ttk.Button(row_top, text="🔄", width=3,
                   command=self._refresh_batch_account_list).pack(side=tk.LEFT, padx=(0, 16))

        # 拆分为两个拉取按钮
        self.btn_batch_fetch = tk.Button(
            row_top, text="📥 已上架",
            command=lambda: self._start_batch_fetch(["0"]),
            font=self.FONT_DEFAULT, bg="#1890ff", fg="black",
        )
        self.btn_batch_fetch.pack(side=tk.LEFT, padx=4)

        self.btn_batch_fetch2 = tk.Button(
            row_top, text="📦 未上架",
            command=lambda: self._start_batch_fetch(["60"]),
            font=self.FONT_DEFAULT, bg="#fa8c16", fg="black",
        )
        self.btn_batch_fetch2.pack(side=tk.LEFT, padx=4)

        self.btn_match_price = tk.Button(
            row_top, text="✨ 全部匹配建议价",
            command=self._match_all_prices,
            font=self.FONT_DEFAULT, bg="#722ed1", fg="black", state="disabled",
        )
        self.btn_match_price.pack(side=tk.LEFT, padx=4)

        self.btn_batch_update = tk.Button(
            row_top, text="✅ 批量改价（勾选项）",
            command=self._start_batch_update,
            font=self.FONT_DEFAULT, bg="#52c41a", fg="black", state="disabled",
        )
        self.btn_batch_update.pack(side=tk.LEFT, padx=4)

        self.btn_batch_stop = tk.Button(
            row_top, text="⏹ 停止",
            command=self._stop_batch_update,
            font=self.FONT_DEFAULT, bg="#ff4d4f", fg="black", state="disabled",
        )
        self.btn_batch_stop.pack(side=tk.LEFT, padx=4)

        tk.Button(
            row_top, text="📋 商品管理",
            command=self._open_import_window,
            font=self.FONT_DEFAULT, bg="#fa8c16", fg="black",
        ).pack(side=tk.LEFT, padx=(16, 4))

        # ── 搜索过滤栏 ────────────────────────────────────────────────────────
        row_search = tk.Frame(frm_ctrl)
        row_search.pack(fill=tk.X, pady=(4, 0))
        tk.Label(row_search, text="🔍 过滤:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.batch_search_var = tk.StringVar()
        self.batch_search_var.trace_add("write", lambda *_: (
            setattr(self, "_batch_page", 0),
            self._render_batch_tree(),
        ))
        ent_search = ttk.Entry(row_search, textvariable=self.batch_search_var,
                               font=self.FONT_DEFAULT, width=28)
        ent_search.pack(side=tk.LEFT, padx=(4, 8))
        tk.Label(row_search, text="（质检码 / 型号 / 颜色 / 成色）",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT)
        ttk.Button(row_search, text="✕ 清除",
                   command=lambda: self.batch_search_var.set("")).pack(side=tk.LEFT, padx=4)

        # ── 编码输入区（质检码 / IMEI 批量查询）────────────────────────────────
        frm_codes = tk.LabelFrame(
            frm_ctrl, text=" 按质检码 / IMEI 查询（每行一个，支持混合输入）",
            font=self.FONT_DEFAULT, fg="#d46b08", padx=8, pady=6,
        )
        frm_codes.pack(fill=tk.X, pady=(8, 4))

        # 输入框 + 滚动条
        code_input_frame = tk.Frame(frm_codes)
        code_input_frame.pack(fill=tk.X)
        self.txt_code_input = tk.Text(
            code_input_frame, height=4,
            font=("Menlo", 12), bg="#fafafa", fg="#000",
            relief="solid", bd=1, padx=6, pady=4,
            wrap="none",
        )
        code_sb = ttk.Scrollbar(code_input_frame, orient="vertical",
                                command=self.txt_code_input.yview)
        self.txt_code_input.configure(yscrollcommand=code_sb.set)
        self.txt_code_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        code_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # 占位提示
        PLACEHOLDER = "993801754\n354347188143710\n993602740\n..."
        self.txt_code_input.insert("1.0", PLACEHOLDER)
        self.txt_code_input.config(fg="#aaaaaa")

        def _on_focus_in(_e):
            if self.txt_code_input.get("1.0", tk.END).strip() == PLACEHOLDER.strip():
                self.txt_code_input.delete("1.0", tk.END)
                self.txt_code_input.config(fg="#000000")

        def _on_focus_out(_e):
            if not self.txt_code_input.get("1.0", tk.END).strip():
                self.txt_code_input.insert("1.0", PLACEHOLDER)
                self.txt_code_input.config(fg="#aaaaaa")

        self.txt_code_input.bind("<FocusIn>",  _on_focus_in)
        self.txt_code_input.bind("<FocusOut>", _on_focus_out)

        # 按钮行
        row_code_btn = tk.Frame(frm_codes)
        row_code_btn.pack(fill=tk.X, pady=(6, 0))
        self.btn_code_fetch = tk.Button(
            row_code_btn, text="🔍 按编码查询并追加",
            command=self._start_code_fetch,
            font=self.FONT_DEFAULT, bg="#fa8c16", fg="black",
        )
        self.btn_code_fetch.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(row_code_btn, text="🗑 清空输入",
                   command=lambda: (
                       self.txt_code_input.delete("1.0", tk.END),
                       self.txt_code_input.config(fg="#000000"),
                   )).pack(side=tk.LEFT)
        self.lbl_code_status = tk.Label(
            row_code_btn, text="", font=self.FONT_DEFAULT, fg="#8c8c8c"
        )
        self.lbl_code_status.pack(side=tk.LEFT, padx=12)

        # 全选 / 反选
        row_sel = tk.Frame(frm_ctrl)
        row_sel.pack(fill=tk.X, pady=(8, 2))
        ttk.Button(row_sel, text="☑ 全选", command=self._batch_select_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_sel, text="☐ 全不选", command=self._batch_deselect_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_sel, text="↓ 仅选需降价项", command=self._batch_select_cheaper).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_sel, text="✨ 仅选已匹配项", command=self._batch_select_matched).pack(side=tk.LEFT, padx=4)
        tk.Button(
            row_sel, text="🗑 清空列表",
            command=self._batch_clear,
            font=self.FONT_DEFAULT, bg="#ff4d4f", fg="black",
        ).pack(side=tk.LEFT, padx=8)
        self.lbl_batch_status = tk.Label(
            row_sel, text="", font=self.FONT_DEFAULT, fg="#8c8c8c"
        )
        self.lbl_batch_status.pack(side=tk.LEFT, padx=12)

        # 进度条
        self.batch_progress_var = tk.DoubleVar()
        self.batch_progress = ttk.Progressbar(
            frm_ctrl, variable=self.batch_progress_var, maximum=100, mode="determinate"
        )
        self.batch_progress.pack(fill=tk.X, pady=(6, 0))

        # ── 状态 Tag 筛选栏 ──────────────────────────────────────────────────
        self.frm_tags = tk.Frame(tab, bg="#f0f0f0", pady=4)
        self.frm_tags.pack(fill=tk.X, padx=12)
        tk.Label(
            self.frm_tags, text="状态筛选：", font=self.FONT_DEFAULT,
            bg="#f0f0f0",
        ).pack(side=tk.LEFT, padx=(0, 6))
        # Tag 按钮区（动态生成，初始为空）
        self.frm_tag_btns = tk.Frame(self.frm_tags, bg="#f0f0f0")
        self.frm_tag_btns.pack(side=tk.LEFT, fill=tk.X)
        tk.Label(
            self.frm_tags, text="（拉取数据后自动生成）",
            font=("PingFang SC", 11), fg="#aaaaaa", bg="#f0f0f0",
        ).pack(side=tk.LEFT, padx=6)

        # ── 商品列表 Treeview ─────────────────────────────────────────────────
        frm_tree = tk.Frame(tab)
        frm_tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        style = ttk.Style()
        style.configure("Batch.Treeview",
                        rowheight=28,
                        font=("PingFang SC", 12),
                        background="#ffffff",
                        fieldbackground="#ffffff")
        style.configure("Batch.Treeview.Heading",
                        font=("PingFang SC", 12, "bold"))

        cols = ("check", "status_name", "qc_code", "title", "condition", "capacity", "color", "cur_price", "settle", "sug_price", "diff", "price_state", "floor")
        self.batch_tree = ttk.Treeview(
            frm_tree, columns=cols, show="headings",
            selectmode="browse", style="Batch.Treeview"
        )
        col_cfg = [
            ("check",       "✓",       40,  "center"),
            ("status_name", "状态",     70,  "center"),
            ("qc_code",     "质检码",  100,  "center"),
            ("title",       "标题",    160,  "w"),
            ("condition",   "成色",     90,  "center"),
            ("capacity",    "容量",     65,  "center"),
            ("color",       "颜色",     75,  "center"),
            ("cur_price",   "售价",     60,  "center"),
            ("settle",      "到手价",   65,  "center"),
            ("sug_price",   "建议价",   65,  "center"),
            ("diff",        "差价",     50,  "center"),
            ("price_state", "动销",     60,  "center"),
            ("floor",       "底价",    110,  "center"),
        ]
        for cid, heading, width, anchor in col_cfg:
            self.batch_tree.heading(cid, text=heading)
            self.batch_tree.column(cid, width=width, anchor=anchor, stretch=(cid == "title"))

        # 颜色标签 —— 背景加深、前景统一黑色保证可读
        self.batch_tree.tag_configure("cheaper",    background="#fff1f0", foreground="#4a0a00")  # 需降价（淡红）
        self.batch_tree.tag_configure("higher",     background="#f0fae8", foreground="#135200")  # 可涨价（淡绿）
        self.batch_tree.tag_configure("ok",         background="#eff6ff", foreground="#003a8c")  # 已最优（淡蓝）
        self.batch_tree.tag_configure("no_data",    background="#f5f5f5", foreground="#595959")  # 无数据（淡灰）
        self.batch_tree.tag_configure("low_conf",   background="#fffbe6", foreground="#614700")  # 低置信度（淡黄）
        self.batch_tree.tag_configure("below_cost", background="#fff7e6", foreground="#7c3500")  # 低于成本底价（淡橙）

        vsb = ttk.Scrollbar(frm_tree, orient="vertical", command=self.batch_tree.yview)
        self.batch_tree.configure(yscrollcommand=vsb.set)
        self.batch_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 点击 ✓ 列切换勾选
        self.batch_tree.bind("<ButtonRelease-1>", self._batch_tree_click)
        self.batch_tree.bind("<Double-1>",        self._batch_tree_manual_price)

        # ── 分页控制栏 ────────────────────────────────────────────────────────
        frm_page = tk.Frame(tab, bg="#f5f5f5", pady=4)
        frm_page.pack(fill=tk.X, padx=12)
        self._batch_page      = 0        # 当前页（0-indexed）
        self._batch_page_size = 50

        self.btn_page_prev = ttk.Button(frm_page, text="◀ 上一页",
                                        command=self._batch_prev_page)
        self.btn_page_prev.pack(side=tk.LEFT, padx=4)
        self.lbl_page_info = tk.Label(frm_page, text="第 1 页 / 共 1 页",
                                      font=self.FONT_DEFAULT, bg="#f5f5f5")
        self.lbl_page_info.pack(side=tk.LEFT, padx=8)
        self.btn_page_next = ttk.Button(frm_page, text="下一页 ▶",
                                        command=self._batch_next_page)
        self.btn_page_next.pack(side=tk.LEFT, padx=4)

        # 每页条数选择
        tk.Label(frm_page, text="每页:", font=self.FONT_DEFAULT,
                 bg="#f5f5f5").pack(side=tk.LEFT, padx=(16, 2))
        self._page_size_var = tk.StringVar(value="50")
        cb_size = ttk.Combobox(frm_page, textvariable=self._page_size_var,
                               values=["25", "50", "100"], width=5,
                               state="readonly", font=self.FONT_DEFAULT)
        cb_size.pack(side=tk.LEFT)
        cb_size.bind("<<ComboboxSelected>>", lambda _: (
            setattr(self, "_batch_page", 0),
            setattr(self, "_batch_page_size", int(self._page_size_var.get())),
            self._render_batch_tree(),
        ))

        self.lbl_page_count = tk.Label(frm_page, text="共 0 件",
                                       font=self.FONT_DEFAULT, bg="#f5f5f5", fg="#888")
        self.lbl_page_count.pack(side=tk.LEFT, padx=16)

        # ── 底部日志 ──────────────────────────────────────────────────────────
        self.batch_log = tk.Text(
            tab, height=5, bg="#2b2b2b", fg="#a9b7c6",
            font=self.FONT_MONO, padx=8, pady=6,
        )
        self.batch_log.pack(fill=tk.X, padx=12, pady=(0, 8))

        self._refresh_batch_account_list()

    # ── 批量：账号刷新 ────────────────────────────────────────────────────────
    def _refresh_batch_account_list(self):
        names = [a.name for a in self.account_mgr.accounts]
        self.combo_batch_account["values"] = names
        if names and not self.combo_batch_account.get():
            self.combo_batch_account.set(names[0])

    def _get_batch_cookie(self) -> Optional[str]:
        name = self.combo_batch_account.get()
        for a in self.account_mgr.accounts:
            if a.name == name:
                return a.cookie
        return None

    # ── 批量：日志 ───────────────────────────────────────────────────────────
    def _batch_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: (
            self.batch_log.insert(tk.END, f"[{ts}] {msg}\n"),
            self.batch_log.see(tk.END),
        ))

    # ── 批量：重建状态 Tag 筛选栏 ────────────────────────────────────────────
    # 状态颜色映射（可按需扩展）
    _STATUS_COLORS = {
        "已上架":  ("#389e0d", "#f6ffed"),   # 绿
        "已下架":  ("#8c8c8c", "#f5f5f5"),   # 灰
        "质检中":  ("#1976d2", "#e3f2fd"),   # 蓝
        "待上架":  ("#d46b08", "#fff7e6"),   # 橙
        "已售出":  ("#9e3d0d", "#fff1f0"),   # 红棕
        "退货中":  ("#cf1322", "#fff1f0"),   # 红
    }

    def _rebuild_status_tags(self):
        """根据当前 _batch_items 的实际状态动态生成 Tag 按钮"""
        # 清空旧按钮
        for w in self.frm_tag_btns.winfo_children():
            w.destroy()
        # 清空提示标签（如有）
        for w in self.frm_tags.winfo_children():
            if isinstance(w, tk.Label) and "拉取数据后" in (w.cget("text") or ""):
                w.destroy()

        # 统计每个状态的数量
        from collections import Counter
        counts = Counter(item.status_name for item in self._batch_items)

        self._status_filters.clear()
        for status_name, count in sorted(counts.items()):
            fg, bg = self._STATUS_COLORS.get(status_name, ("#555555", "#eeeeee"))
            var = tk.BooleanVar(value=True)   # 默认全部勾选显示
            self._status_filters[status_name] = var

            cb = tk.Checkbutton(
                self.frm_tag_btns,
                text=f"{status_name} ({count})",
                variable=var,
                font=("PingFang SC", 12, "bold"),
                fg=fg, bg=bg,
                selectcolor=bg,
                activeforeground=fg,
                activebackground=bg,
                relief="solid", bd=1,
                padx=8, pady=3,
                command=self._render_batch_tree,
            )
            cb.pack(side=tk.LEFT, padx=4, pady=2)

    # ── 批量：Treeview 渲染 ──────────────────────────────────────────────────
    def _render_batch_tree(self):
        """将 self._batch_items 按 Tag 过滤 + 搜索关键字过滤后渲染到 Treeview"""
        self.batch_tree.delete(*self.batch_tree.get_children())

        # 状态 Tag 过滤
        active   = {name for name, var in self._status_filters.items() if var.get()}
        show_all = not active

        # 搜索关键字过滤
        kw = getattr(self, "batch_search_var", None)
        kw = kw.get().strip().lower() if kw else ""

        visible_indices = []
        for idx, item in enumerate(self._batch_items):
            if not show_all and item.status_name not in active:
                continue
            if kw and not any(kw in s.lower() for s in [
                item.qc_code, item.imei, item.title,
                item.model, item.condition, item.color, item.capacity,
            ]):
                continue
            visible_indices.append(idx)

        # 分页
        page_size  = getattr(self, "_batch_page_size", 50)
        page       = getattr(self, "_batch_page", 0)
        total_vis  = len(visible_indices)
        total_pages = max(1, (total_vis + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        self._batch_page = page

        page_indices = visible_indices[page * page_size : (page + 1) * page_size]

        for idx in page_indices:
            item = self._batch_items[idx]
            check_mark = "✓" if item.checked else "○"
            diff = item.suggested_price - int(item.current_price) if item.suggested_price else 0

            if item.suggested_price == 0:
                tag = "no_data"; sug_txt = "--"; diff_txt = "--"
            elif diff < 0:
                tag = "low_conf" if item.low_confidence else "cheaper"
                sug_txt = str(item.suggested_price); diff_txt = str(diff)
            elif diff > 0:
                tag = "low_conf" if item.low_confidence else "higher"
                sug_txt = str(item.suggested_price); diff_txt = f"+{diff}"
            else:
                tag = "low_conf" if item.low_confidence else "ok"
                sug_txt = str(item.suggested_price); diff_txt = "±0"

            if item.suggested_price == 0:
                price_state = "无数据"
            elif item.low_confidence:
                price_state = "⚠️低置信"
            elif item.is_fast:
                price_state = "🔥极速"
            else:
                price_state = "🐢保守"

            # 底价列：显示成本底价（ERP优先）或统计P20
            # 对比对象是「改价后预计到手价」，低于底价只标注警告，不干预勾选
            effective_floor = item.cost_floor or item.floor_price
            cur = int(item.current_price) if item.current_price else 0
            if effective_floor:
                floor_src   = "💰" if item.cost_floor else "📊"
                floor_val   = item.cost_floor if item.cost_floor else item.floor_price
                floor_label = f"底¥{floor_val}"

                # 用建议价估算改价后到手价（建议价存在时），否则用当前售价
                ref_price  = item.suggested_price if item.suggested_price else cur
                est_settle = _estimate_settle(ref_price, item.settle_price, item.current_price) if ref_price else 0

                if est_settle and est_settle < floor_val:
                    # 到手价 < 底价：橙色警告标注，不改行背景（用户需自行判断）
                    floor_note = f"⚠️{floor_src}{floor_label}（到手¥{est_settle}亏）"
                    if tag not in ("low_conf", "below_cost"):
                        tag = "below_cost"
                else:
                    settle_disp = f"到手¥{est_settle}" if est_settle else ""
                    floor_note = f"{floor_src}{floor_label}" + (f" {settle_disp}" if settle_disp else "")
            else:
                floor_note = "--"

            self.batch_tree.insert(
                "", tk.END, iid=str(idx),
                values=(
                    check_mark,
                    item.status_name,
                    item.qc_code or item.imei or "--",
                    item.title,
                    item.condition,
                    item.capacity,
                    item.color,
                    cur or "--",
                    int(item.settle_price) if item.settle_price else "--",
                    sug_txt,
                    diff_txt,
                    price_state,
                    floor_note,
                ),
                tags=(tag,),
            )

        checked = sum(1 for i in self._batch_items if i.checked)
        self.lbl_batch_status.config(
            text=f"共 {len(self._batch_items)} 件，筛选 {total_vis} 件，已勾选 {checked} 件"
        )
        # 更新分页标签和按钮状态
        if hasattr(self, "lbl_page_info"):
            self.lbl_page_info.config(text=f"第 {page+1} 页 / 共 {total_pages} 页")
            self.lbl_page_count.config(text=f"筛选 {total_vis} 件")
            self.btn_page_prev.config(state="normal" if page > 0 else "disabled")
            self.btn_page_next.config(state="normal" if page < total_pages - 1 else "disabled")

    # ── 批量：点击切换勾选 ───────────────────────────────────────────────────
    def _batch_tree_click(self, event):
        col = self.batch_tree.identify_column(event.x)
        row = self.batch_tree.identify_row(event.y)
        if not row or col != "#1":   # 只响应第一列（✓）
            return
        idx = int(row)
        self._batch_items[idx].checked = not self._batch_items[idx].checked
        self._render_batch_tree()

    def _batch_tree_manual_price(self, event):
        """双击任意行 → 弹出手动定价小窗口"""
        col = self.batch_tree.identify_column(event.x)
        row = self.batch_tree.identify_row(event.y)
        if not row or col == "#1":   # 排除第一列（✓那列是勾选，不触发）
            return
        idx = int(row)
        item = self._batch_items[idx]
        # 双击「动销」列（#12）→ 显示权重明细；其他列 → 手动定价弹窗
        if col == "#12":
            self._open_batch_weight_detail(item)
        else:
            self._open_manual_price_popup(item, idx)

    def _open_batch_weight_detail(self, item: "BatchItem"):
        """批量列表权重明细弹窗：显示该行商品的成交样本和权重分布"""
        if self.engine is None:
            messagebox.showinfo("提示", "请先同步数据"); return

        now    = pd.Timestamp.now()
        cutoff = now - pd.Timedelta(days=ENGINE_WINDOW)

        def _filter(with_color):
            mask = (
                (self.engine.df["型号"]    == item.model)
                & (self.engine.df["精确成色"] == item.condition)
                & (self.engine.df["容量"]    == item.capacity)
            )
            if with_color:
                mask &= (self.engine.df["颜色"] == item.color)
            if "售出时间" in self.engine.df.columns:
                mask &= (self.engine.df["售出时间"] >= cutoff)
            return self.engine.df[mask].copy()

        sub = _filter(True)
        color_fallback = sub.empty
        if color_fallback:
            sub = _filter(False)

        if sub.empty:
            messagebox.showinfo("无数据", f"{item.model} {item.condition} {item.capacity} 在60天内无成交记录")
            return

        age_days     = (now - sub["售出时间"]).dt.total_seconds() / 86400
        sub          = sub.copy()
        sub["_age"]  = age_days.round(1)
        sub["_w"]    = age_days.apply(lambda d: ENGINE_W_RECENT if d <= ENGINE_NEAR else ENGINE_W_OLD).astype(int)
        sub["_fast"] = (sub["sales_hours"] <= FAST_SALE_HOURS) if "sales_hours" in sub.columns else False
        sub          = sub.sort_values("售出时间", ascending=False)

        near_n  = int((sub["_w"] == ENGINE_W_RECENT).sum())
        old_n   = int((sub["_w"] == ENGINE_W_OLD).sum())
        fast_n  = int(sub["_fast"].sum())
        total_w = near_n * ENGINE_W_RECENT + old_n * ENGINE_W_OLD

        win = tk.Toplevel(self.root)
        win.title(f"权重明细 — {item.model} {item.condition} {item.capacity} {item.color}")
        win.geometry("780x500")
        win.lift()

        # ── 汇总卡片 ──────────────────────────────────────────────────────────
        card = tk.Frame(win, bg="#f5f5f5", pady=8)
        card.pack(fill=tk.X, padx=10, pady=(8, 2))

        def _kv(parent, label, val, fg="#222"):
            f = tk.Frame(parent, bg="#f5f5f5")
            f.pack(side=tk.LEFT, padx=16)
            tk.Label(f, text=label, font=("PingFang SC", 10), fg="#888", bg="#f5f5f5").pack()
            tk.Label(f, text=val,   font=("PingFang SC", 14, "bold"), fg=fg, bg="#f5f5f5").pack()

        _kv(card, "原始样本",      f"{len(sub)} 条")
        _kv(card, f"近{ENGINE_NEAR}天 ×{ENGINE_W_RECENT}", f"{near_n} 条", "#1890ff")
        _kv(card, f"{ENGINE_NEAR}-{ENGINE_WINDOW}天 ×{ENGINE_W_OLD}", f"{old_n} 条", "#888")
        _kv(card, "加权总样本",    f"{total_w} 个")
        _kv(card, "24h极速成交",   f"{fast_n} 条", "#52c41a" if fast_n else "#888")
        _kv(card, "建议价",        f"¥{item.suggested_price}" if item.suggested_price else "--",
            "#d32f2f" if item.is_fast else "#f57c00")

        if color_fallback:
            tk.Label(win, text="⚠️ 该颜色无精确匹配，已降级忽略颜色参与计算",
                     font=("PingFang SC", 11), fg="#fa8c16").pack(pady=(0, 4))

        # ── 明细表格 ──────────────────────────────────────────────────────────
        cols = ("sold_time", "age", "weight", "sell_price", "hours", "fast")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=16)
        tree.heading("sold_time",  text="成交时间");    tree.column("sold_time",  width=140, anchor="center")
        tree.heading("age",        text="距今");        tree.column("age",        width=75,  anchor="center")
        tree.heading("weight",     text="权重");        tree.column("weight",     width=100, anchor="center")
        tree.heading("sell_price", text="成交价");      tree.column("sell_price", width=90,  anchor="center")
        tree.heading("hours",      text="动销时长");    tree.column("hours",      width=90,  anchor="center")
        tree.heading("fast",       text="极速?");       tree.column("fast",       width=60,  anchor="center")

        tree.tag_configure("recent", background="#e6f7ff")
        tree.tag_configure("fast",   background="#f6ffed")

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=6)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, pady=6, padx=(0, 6))

        for _, row in sub.iterrows():
            sold_s  = row["售出时间"].strftime("%Y-%m-%d %H:%M") if pd.notna(row["售出时间"]) else "--"
            age_s   = f"{row['_age']:.0f}天"
            w_s     = f"×{int(row['_w'])}  {'近期' if row['_w'] == ENGINE_W_RECENT else '较早'}"
            price_s = f"¥{int(row['最终售价'])}"
            hours_s = f"{row['sales_hours']:.1f}h" if "sales_hours" in sub.columns else "--"
            fast_s  = "🔥" if row["_fast"] else "—"
            tag     = "fast" if row["_fast"] else ("recent" if row["_w"] == ENGINE_W_RECENT else "")
            tree.insert("", tk.END, values=(sold_s, age_s, w_s, price_s, hours_s, fast_s), tags=(tag,))

    def _open_manual_price_popup(self, item: "BatchItem", idx: int):
        """手动定价弹窗：适用于无历史数据或想覆盖建议价的情况"""
        popup = tk.Toplevel(self.root)
        popup.title("✏️ 手动定价")
        popup.geometry("380x280")
        popup.resizable(False, False)
        popup.grab_set()   # 模态

        # 机器信息
        info = tk.LabelFrame(popup, text=" 商品信息 ", font=("PingFang SC", 12, "bold"),
                             padx=10, pady=6)
        info.pack(fill=tk.X, padx=14, pady=(12, 6))
        tk.Label(info, text=f"{item.model}  {item.condition}  {item.capacity}  {item.color}",
                 font=("PingFang SC", 13), fg="#222").pack(anchor="w")
        tk.Label(info, text=f"质检码：{item.qc_code or item.imei or '—'}   状态：{item.status_name}",
                 font=("PingFang SC", 11), fg="#888").pack(anchor="w", pady=(2, 0))
        if item.current_price:
            tk.Label(info, text=f"当前售价：¥{int(item.current_price)}",
                     font=("PingFang SC", 11), fg="#555").pack(anchor="w")

        # 价格输入
        input_frm = tk.Frame(popup); input_frm.pack(fill=tk.X, padx=14, pady=6)
        tk.Label(input_frm, text="设定价格：¥", font=("PingFang SC", 14, "bold")).pack(side=tk.LEFT)
        price_var = tk.StringVar(value=str(int(item.suggested_price)) if item.suggested_price else "")
        price_entry = ttk.Entry(input_frm, textvariable=price_var, width=8,
                                font=("PingFang SC", 16, "bold"))
        price_entry.pack(side=tk.LEFT, padx=4)
        price_entry.focus_set()
        price_entry.select_range(0, tk.END)

        # 实时预估到手价
        settle_lbl = tk.Label(popup, text="", font=("PingFang SC", 12), fg="#389e0d")
        settle_lbl.pack(pady=(0, 4))

        def _update_settle(*_):
            try:
                p = int(price_var.get())
                settle = round(p * (1 - PLATFORM_FEE_RATE) - STATION_SERVICE_FEE)
                settle_lbl.config(text=f"💰 预计到手：¥{settle}  （售价×{int((1-PLATFORM_FEE_RATE)*100)}% − ¥{STATION_SERVICE_FEE}服务费）")
            except Exception:
                settle_lbl.config(text="")
        price_var.trace_add("write", _update_settle)
        _update_settle()

        # 按钮
        btn_frm = tk.Frame(popup); btn_frm.pack(pady=8)

        def _confirm():
            try:
                p = int(price_var.get())
                if p <= 0: raise ValueError
            except Exception:
                messagebox.showwarning("提示", "请输入有效价格", parent=popup); return
            self._batch_items[idx].suggested_price = p
            self._batch_items[idx].is_fast         = False
            self._batch_items[idx].checked         = True   # 自动勾选，方便直接批量改
            popup.destroy()
            self._render_batch_tree()

        tk.Button(btn_frm, text="✅ 确认，加入改价队列",
                  font=("PingFang SC", 13), bg="#52c41a", fg="black",
                  padx=12, command=_confirm).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frm, text="取消",
                  font=("PingFang SC", 12), padx=8,
                  command=popup.destroy).pack(side=tk.LEFT, padx=6)

        # 回车确认
        popup.bind("<Return>", lambda e: _confirm())

    def _batch_prev_page(self):
        self._batch_page = max(0, getattr(self, "_batch_page", 0) - 1)
        self._render_batch_tree()

    def _batch_next_page(self):
        self._batch_page = getattr(self, "_batch_page", 0) + 1
        self._render_batch_tree()

    def _batch_select_all(self):
        for item in self._batch_items:
            item.checked = True
        self._render_batch_tree()

    def _batch_deselect_all(self):
        for item in self._batch_items:
            item.checked = False
        self._render_batch_tree()

    def _batch_select_cheaper(self):
        """仅勾选建议价低于当前价的商品"""
        for item in self._batch_items:
            item.checked = (
                item.suggested_price > 0
                and item.suggested_price < int(item.current_price)
            )
        self._render_batch_tree()

    def _batch_select_matched(self):
        """仅勾选已成功匹配到建议价的商品（无论涨跌）"""
        for item in self._batch_items:
            item.checked = item.suggested_price > 0
        self._render_batch_tree()

    def _batch_clear(self):
        """清空商品列表"""
        if self._batch_items and not messagebox.askyesno("确认", "确定清空全部商品列表？"):
            return
        self._batch_items.clear()
        self.batch_tree.delete(*self.batch_tree.get_children())
        # 清空 tag 栏
        for w in self.frm_tag_btns.winfo_children():
            w.destroy()
        self._status_filters.clear()
        self.btn_match_price.config(state="disabled")
        self.btn_batch_update.config(state="disabled")
        self.lbl_batch_status.config(text="")

    # ── 批量：按编码查询（线程）─────────────────────────────────────────────
    def _parse_raw_to_batch_item(self, raw: dict) -> BatchItem:
        """将接口原始 item dict 转为 BatchItem，供两种拉取方式复用"""
        props  = {p["pnName"]: p["pvName"] for p in (raw.get("properties") or [])}
        grade  = raw.get("gradeInfo") or {}
        p_info = raw.get("priceInfo") or {}
        state  = raw.get("state") or {}
        _status_val = state.get("status") if state.get("status") is not None else raw.get("status")
        status_code = str(_status_val) if _status_val is not None else ""
        status_name = (state.get("statusName") or raw.get("statusDesc") or "未知状态").strip()
        return BatchItem(
            product_id      = str(raw.get("productId") or ""),
            title           = raw.get("title") or "",
            model           = clean_model_name(raw.get("title") or ""),
            condition       = build_condition(raw.get("properties", []), grade),
            capacity        = props.get("存储容量") or props.get("容量") or "未知",
            color           = props.get("颜色") or "未知",
            current_price   = (p_info.get("sellingPrice") or 0) / 100,
            settle_price    = (p_info.get("preSettlePrice") or 0) / 100,
            suggested_price = 0,
            is_fast         = False,
            status_code     = status_code,
            status_name     = status_name,
            qc_code         = str(raw.get("qcCode") or ""),
            imei            = str(raw.get("imei") or ""),
            category_id     = int(raw.get("cateId") or 0),
            brand_id        = int(raw.get("brandId") or 0),
            model_id        = int(raw.get("modelId") or 0),
            grade_name      = raw.get("gradeName") or "",
            product_params  = _build_product_params(raw.get("properties") or []),
        )

    def _start_code_fetch(self):
        cookie = self._get_batch_cookie()
        if not cookie:
            messagebox.showwarning("提示", "请先选择操作店铺")
            return

        raw_text = self.txt_code_input.get("1.0", tk.END).strip()
        # 支持每行一个 或 逗号/分号分隔
        lines = [c.strip() for c in re.split(r"[\s,，;；]+", raw_text) if c.strip()]
        codes = [ln for ln in lines if not ln.startswith("...")]
        if not codes:
            messagebox.showwarning("提示", "请在输入框中填入质检码或 IMEI，每行一个")
            return

        # 去重
        seen = set()
        unique_codes = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                unique_codes.append(c)

        self.btn_code_fetch.config(state="disabled", text="🔍 查询中...")
        self.btn_batch_fetch.config(state="disabled")
        self.lbl_code_status.config(text=f"准备查询 {len(unique_codes)} 个编码...", fg="#1976d2")
        self.batch_progress_var.set(0)

        def _progress(done, total):
            pct = done / total * 100 if total else 0
            self.root.after(0, lambda p=pct, d=done, t=total: (
                self.batch_progress_var.set(p),
                self.lbl_code_status.config(text=f"正在查询 {d}/{t}..."),
            ))

        def _worker():
            try:
                raw_items, not_found = ImeiService(cookie).fetch_by_codes(
                    unique_codes, on_progress=_progress
                )
            except requests.RequestException as e:
                err = str(e)
                self.root.after(0, lambda m=err: (
                    self.btn_code_fetch.config(state="normal", text="🔍 按编码查询并追加"),
                    self.btn_batch_fetch.config(state="normal"),
                    self.lbl_code_status.config(text=f"❌ {m}", fg="#cf1322"),
                ))
                return

            new_items = [self._parse_raw_to_batch_item(r) for r in raw_items]

            # 追加到现有列表（按 product_id 去重）
            existing_ids = {it.product_id for it in self._batch_items}
            added = [it for it in new_items if it.product_id not in existing_ids]

            def _done():
                self._batch_items.extend(added)
                self.batch_progress_var.set(100)
                self.btn_code_fetch.config(state="normal", text="🔍 按编码查询并追加")
                self.btn_batch_fetch.config(state="normal")
                self.btn_match_price.config(state="normal")
                if self._batch_items:
                    self.btn_batch_update.config(state="normal")
                self._rebuild_status_tags()
                self._render_batch_tree()

                summary = f"✅ 新增 {len(added)} 件"
                if not_found:
                    summary += f"，未找到 {len(not_found)} 个编码"
                    self._batch_log(f"⚠️ 未找到的编码：{', '.join(not_found[:20])}"
                                   + ("..." if len(not_found) > 20 else ""))
                self.lbl_code_status.config(text=summary, fg="#389e0d")
                self._batch_log(f"🔍 按编码查询完成：{summary}")

            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_batch_fetch(self, status_list: list[str] = None):
        cookie = self._get_batch_cookie()
        if not cookie:
            messagebox.showwarning("提示", "请先选择操作店铺")
            return

        label = "未上架" if status_list == ["60"] else "已上架"
        self.btn_batch_fetch.config(state="disabled")
        self.btn_batch_fetch2.config(state="disabled")
        self.btn_match_price.config(state="disabled")
        self.btn_batch_update.config(state="disabled")
        self.batch_progress_var.set(0)
        self._batch_items.clear()
        self.batch_tree.delete(*self.batch_tree.get_children())

        def _progress(page, count):
            self.root.after(0, lambda p=page, c=count: (
                self.batch_progress_var.set(min(page / MAX_PAGES * 100, 99)),
                self.lbl_batch_status.config(text=f"正在拉取{label}第 {p} 页，已获取 {c} 件..."),
            ))

        def _worker():
            svc = ImeiService(cookie)
            try:
                raw_items = svc.fetch_on_sale(on_progress=_progress, status_list=status_list)
            except Exception as e:
                self.root.after(0, lambda err=e: (
                    self.btn_batch_fetch.config(state="normal"),
                    self.btn_batch_fetch2.config(state="normal"),
                    self.lbl_batch_status.config(text=f"❌ 拉取失败：{err}", fg="#cf1322"),
                    self._batch_log(f"❌ 拉取失败：{err}"),
                ))
                return

            batch_items = [self._parse_raw_to_batch_item(r) for r in raw_items]

            def _done():
                self._batch_items = batch_items
                self.batch_progress_var.set(100)
                self.btn_batch_fetch.config(state="normal")
                self.btn_batch_fetch2.config(state="normal")
                self.btn_match_price.config(state="normal")
                if self._batch_items:
                    self.btn_batch_update.config(state="normal")
                self._rebuild_status_tags()
                self._render_batch_tree()
                self._batch_log(f"✅ {label} 拉取完成，共 {len(batch_items)} 件")

            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    # ── 批量：匹配建议价 ─────────────────────────────────────────────────────
    def _match_all_prices(self):
        if not self._batch_items:
            return
        if self.engine is None:
            messagebox.showwarning("提示", "请先在「数据管理」页同步历史成交数据")
            return

        matched = no_data = 0
        for item in self._batch_items:
            result = self.engine.query(
                model=item.model,
                condition=item.condition,
                capacity=item.capacity,
                color=item.color,
            )
            if result:
                item.suggested_price = result.suggested_price
                item.is_fast         = result.is_fast
                item.low_confidence  = result.low_confidence
                item.floor_price     = result.floor_price
                matched += 1
            else:
                item.suggested_price = 0
                item.low_confidence  = False
                item.floor_price     = 0
                no_data += 1

            # 成本底价（优先级高于统计P20）
            cost_f = self.cost_price_map.get_fuzzy(item.model, item.color, item.capacity)
            item.cost_floor = int(cost_f) if cost_f else 0

        # 默认只勾选需要降价的（不因成本底价屏蔽，亏损出货由用户自行判断）
        below_cost_count = 0
        for item in self._batch_items:
            item.checked = (
                item.suggested_price > 0
                and item.suggested_price < int(item.current_price)
            )
            # 统计到手价低于成本底价的件数（仅用于日志提示，不影响勾选）
            if item.cost_floor and item.suggested_price > 0:
                est_settle = _estimate_settle(item.suggested_price, item.settle_price, item.current_price)
                if est_settle < item.cost_floor:
                    below_cost_count += 1

        self._render_batch_tree()
        self.btn_batch_update.config(state="normal")
        below_note = f"，⚠️ {below_cost_count} 件改价后到手价低于成本底价（请留意底价列）" if below_cost_count else ""
        self._batch_log(
            f"✨ 匹配完成：{matched} 件有建议价，{no_data} 件无历史数据；"
            f"已自动勾选需降价项{below_note}"
        )

    # ── 批量：执行改价（线程）────────────────────────────────────────────────
    def _start_batch_update(self):
        cookie = self._get_batch_cookie()
        if not cookie:
            messagebox.showwarning("提示", "请先选择操作店铺")
            return

        # 只过滤勾选+有建议价+价格不同的商品，不按状态名预过滤（让接口判断）
        targets = [
            item for item in self._batch_items
            if item.checked and item.suggested_price > 0
               and item.suggested_price != int(item.current_price)
        ]

        if not targets:
            messagebox.showinfo("提示", "没有需要改价的商品（请先匹配建议价并勾选）")
            return

        confirm = messagebox.askyesno(
            "确认批量改价",
            f"即将对 {len(targets)} 件商品执行改价\n\n"
            "• 每件改价间隔 1 秒，避免触发风控\n"
            "• 非在架商品会自动跳过\n"
            "• 改价后无法撤销\n\n确认开始？",
        )
        if not confirm:
            return

        self.btn_batch_update.config(state="disabled", text="⏳ 批量改价中...")
        self.btn_batch_fetch.config(state="disabled")
        self.btn_match_price.config(state="disabled")
        self.btn_batch_stop.config(state="normal")
        self.batch_progress_var.set(0)
        self._batch_stop_flag = False

        svc = ImeiService(cookie)

        def _worker():
            total = len(targets)
            success = fail = skipped_count = stopped = 0
            for i, item in enumerate(targets):

                # ── 检查停止标志 ──────────────────────────────────────────
                if self._batch_stop_flag:
                    stopped = total - i
                    self._batch_log(f"⏹ 用户手动停止，剩余 {stopped} 件已取消")
                    break

                pct = i / total * 100
                self.root.after(0, lambda p=pct, t=item: (
                    self.batch_progress_var.set(p),
                    self.lbl_batch_status.config(
                        text=f"正在改价 {i+1}/{total}：{t.title[:18]}..."
                    ),
                ))

                # 构造临时 ProductDetail 供 update_price 使用
                pd_obj = ProductDetail(
                    product_id    = item.product_id,
                    sku_id        = "",
                    group_key     = "",
                    title         = item.title,
                    model         = item.model,
                    condition     = item.condition,
                    capacity      = item.capacity,
                    color         = item.color,
                    current_price = item.current_price,
                    settle_price  = 0,
                    status        = item.status_code,
                )
                try:
                    result_msg = svc.update_price(pd_obj, float(item.suggested_price))
                    old_price = int(item.current_price)

                    # 改价成功后立刻从转转接口拿真实 preSettlePrice
                    real_settle = svc.fetch_settle_price(item.product_id)
                    if real_settle is not None:
                        new_settle = int(real_settle)
                        settle_note = f"💰到手¥{new_settle}（转转实时）"
                    else:
                        # 接口拉不到就用改价前的比例估算，标注是估算
                        if item.settle_price and item.current_price:
                            fee_rate = 1 - (item.settle_price / item.current_price)
                        else:
                            fee_rate = PLATFORM_FEE_RATE
                        new_settle = round(item.suggested_price * (1 - fee_rate) - STATION_SERVICE_FEE)
                        settle_note = f"💰到手约¥{new_settle}（估算）"

                    item.current_price = float(item.suggested_price)
                    item.settle_price  = float(new_settle)
                    item.checked = False
                    success += 1
                    action = "未上架→已上架  定价" if pd_obj.status == "60" else "改价"
                    diff = item.suggested_price - old_price
                    diff_str = f"（{'↓' if diff < 0 else '↑'}{abs(diff)}元）"
                    self._batch_log(
                        f"✅ [{i+1}/{total}] [{item.qc_code or item.imei or '—'}]  "
                        f"{item.model} {item.condition} {item.capacity}  "
                        f"{action}  ¥{old_price} → ¥{item.suggested_price} {diff_str}  "
                        f"{settle_note}"
                    )
                except ValueError as e:
                    err = str(e)
                    if "不可改价" in err or "下架" in err or "质检" in err or "校验" in err:
                        skipped_count += 1
                        self._batch_log(f"⏭ [{i+1}/{total}] {item.title[:20]} 跳过：{err}")
                    else:
                        fail += 1
                        self._batch_log(f"❌ [{i+1}/{total}] {item.title[:20]} 失败：{err}")
                except Exception as e:
                    fail += 1
                    self._batch_log(f"❌ [{i+1}/{total}] {item.title[:20]} 网络异常：{e}")

                import time
                time.sleep(1)

            def _done():
                was_stopped = self._batch_stop_flag
                self._batch_stop_flag = False
                self.batch_progress_var.set(100 if not was_stopped else (success + fail + skipped_count) / total * 100)
                self.btn_batch_update.config(state="normal", text="✅ 批量改价（勾选项）")
                self.btn_batch_fetch.config(state="normal")
                self.btn_match_price.config(state="normal")
                self.btn_batch_stop.config(state="disabled")
                self._render_batch_tree()
                summary = f"成功 {success} 件"
                if skipped_count: summary += f"，跳过 {skipped_count} 件"
                if fail:          summary += f"，失败 {fail} 件"
                if was_stopped:   summary += f"，已手动停止（剩余 {stopped} 件未执行）"
                self.lbl_batch_status.config(
                    text=f"{'⏹ 已停止' if was_stopped else '批量改价完成'}：{summary}",
                    fg="#cf1322" if (fail or was_stopped) else "#389e0d",
                )
                detail = f"✅ 成功 {success} 件"
                if skipped_count: detail += f"\n⏭ 跳过 {skipped_count} 件"
                if fail:          detail += f"\n❌ 失败 {fail} 件"
                if was_stopped:   detail += f"\n⏹ 手动停止，剩余 {stopped} 件未执行"
                messagebox.showinfo("完成" if not was_stopped else "已停止",
                                    f"批量改价{'完成' if not was_stopped else '已中止'}\n{detail}")

            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()


    # ── 批量：停止改价 ───────────────────────────────────────────────────────
    def _stop_batch_update(self):
        """设置停止标志，当前正在执行的这一件改完后就停"""
        self._batch_stop_flag = True
        self.btn_batch_stop.config(state="disabled", text="⏳ 停止中...")
        self._batch_log("⏹ 正在停止……当前这件改完后立即中止")

    # ════════════════════════════════════════════════════════════════════════
    # 🤖  自动化标签页
    # ════════════════════════════════════════════════════════════════════════
    def _build_tab_auto(self, nb: ttk.Notebook):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 🤖 自动化 ")

        canvas = tk.Canvas(tab, highlightthickness=0)
        sb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = tk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: (
            canvas.configure(scrollregion=canvas.bbox("all")),
            canvas.itemconfig(win_id, width=canvas.winfo_width()),
        ))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))

        PAD = dict(padx=14, pady=6)

        # ── 共用：操作店铺选择 ─────────────────────────────────────────────
        frm_accs = tk.LabelFrame(inner, text=" 操作店铺（自动化任务使用） ",
                                 font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=8)
        frm_accs.pack(fill=tk.X, **PAD)
        tk.Label(frm_accs, text="选择店铺：", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.combo_auto_account = ttk.Combobox(frm_accs, state="readonly",
                                               font=self.FONT_DEFAULT, width=20)
        self.combo_auto_account.pack(side=tk.LEFT, padx=6)
        ttk.Button(frm_accs, text="🔄 刷新",
                   command=self._refresh_auto_account_list).pack(side=tk.LEFT, padx=4)
        tk.Label(frm_accs, text="（所有自动化任务共用此店铺）",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=8)
        # 日志按钮放在顶部，随时可点
        tk.Button(frm_accs, text="📋 查看日志", font=self.FONT_DEFAULT,
                  bg="#595959", fg="white",
                  command=self._open_auto_log_window).pack(side=tk.RIGHT, padx=4)

        # ── 商品导入区 ────────────────────────────────────────────────────────
        frm_import = tk.LabelFrame(inner, text=" 📋 商品管理",
                                   font=self.FONT_BOLD, fg="#d46b08", padx=10, pady=8)
        frm_import.pack(fill=tk.X, **PAD)

        frm_imp_row = tk.Frame(frm_import); frm_imp_row.pack(fill=tk.X, pady=2)
        tk.Button(frm_imp_row, text="📋 打开商品管理窗口", font=self.FONT_DEFAULT,
                  bg="#fa8c16", fg="black",
                  command=self._open_import_window).pack(side=tk.LEFT, padx=(0, 12))
        self.lbl_auto_import_status = tk.Label(
            frm_imp_row, text="尚未导入任何商品",
            font=self.FONT_DEFAULT, fg="#aaaaaa"
        )
        self.lbl_auto_import_status.pack(side=tk.LEFT)
        canvas.bind_all("<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 模块 0：爱管机 ERP 同步
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        f0 = tk.LabelFrame(inner, text=" 🔗 模块0：爱管机 ERP 同步商品",
                           font=self.FONT_BOLD, fg="#006d75", padx=10, pady=8)
        f0.pack(fill=tk.X, **PAD)

        # Token 配置行
        r0a = tk.Frame(f0); r0a.pack(fill=tk.X, pady=2)
        tk.Label(r0a, text="Authorization:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.erp_token_var = tk.StringVar(value=self.erp_config.token)
        erp_token_entry = ttk.Entry(r0a, textvariable=self.erp_token_var,
                                    font=("Menlo", 11), width=38, show="*")
        erp_token_entry.pack(side=tk.LEFT, padx=6)
        tk.Button(r0a, text="👁", font=self.FONT_DEFAULT, width=2,
                  command=lambda e=erp_token_entry: e.config(
                      show="" if e.cget("show") == "*" else "*")
                  ).pack(side=tk.LEFT, padx=2)
        tk.Button(r0a, text="💾 保存", font=self.FONT_DEFAULT,
                  bg="#1890ff", fg="black",
                  command=self._erp_save_token).pack(side=tk.LEFT, padx=4)
        tk.Button(r0a, text="🔍 验证", font=self.FONT_DEFAULT,
                  command=lambda: threading.Thread(
                      target=self._erp_check_token, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=2)
        self.lbl_erp_token_status = tk.Label(r0a, text="", font=("PingFang SC", 11))
        self.lbl_erp_token_status.pack(side=tk.LEFT, padx=8)

        # Version 行
        r0v = tk.Frame(f0); r0v.pack(fill=tk.X, pady=(0, 2))
        tk.Label(r0v, text="Version:         ", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.erp_version_var = tk.StringVar(value=self.erp_config.version)
        ttk.Entry(r0v, textvariable=self.erp_version_var,
                  font=("Menlo", 11), width=20).pack(side=tk.LEFT, padx=6)
        tk.Label(r0v,
                 text="从抓包 Request Headers 里复制 Version 字段粘贴到此，点💾保存",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=4)

        # 同步选项行
        r0b = tk.Frame(f0); r0b.pack(fill=tk.X, pady=4)
        self.erp_sync_shelf    = tk.BooleanVar(value=True)
        self.erp_sync_stock    = tk.BooleanVar(value=False)
        self.erp_sync_sold     = tk.BooleanVar(value=False)
        tk.Checkbutton(r0b, text="✅ 已上架商品", variable=self.erp_sync_shelf,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Checkbutton(r0b, text="📦 在库库存",   variable=self.erp_sync_stock,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=16)
        tk.Checkbutton(r0b, text="🧾 已售记录",   variable=self.erp_sync_sold,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=0)

        # 操作行一：商品同步
        r0c = tk.Frame(f0); r0c.pack(fill=tk.X, pady=(4, 2))
        tk.Button(r0c, text="▶ 立即从 ERP 同步并导入商品管理", font=self.FONT_DEFAULT,
                  bg="#006d75", fg="white",
                  command=lambda: self._run_with_log(self._erp_sync_and_import)
                  ).pack(side=tk.LEFT, padx=(0, 12))
        tk.Button(r0c, text="🔬 调试（看原始响应）", font=self.FONT_DEFAULT,
                  bg="#531dab", fg="white",
                  command=lambda: threading.Thread(
                      target=self._erp_debug, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=(0, 12))
        self.lbl_erp_sync_status = tk.Label(r0c, text="",
                                            font=("PingFang SC", 11), fg="#888")
        self.lbl_erp_sync_status.pack(side=tk.LEFT)

        # 操作行二：成本底价同步
        r0d = tk.Frame(f0); r0d.pack(fill=tk.X, pady=(2, 4))
        tk.Button(r0d, text="💰 同步成本底价", font=self.FONT_DEFAULT,
                  bg="#d46b08", fg="white",
                  command=lambda: self._run_with_log(self._erp_sync_cost_prices)
                  ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(r0d, text="📋 查看明细", font=self.FONT_DEFAULT,
                  command=self._open_cost_price_viewer
                  ).pack(side=tk.LEFT, padx=(0, 12))
        n_cost = self.cost_price_map.count
        init_txt = (f"📦 已缓存 {n_cost} 个SKU成本底价" if n_cost
                    else "⚪ 尚未同步成本底价（同步后底价列将精确显示）")
        self.lbl_cost_status = tk.Label(r0d, text=init_txt,
                                        font=("PingFang SC", 11),
                                        fg="#389e0d" if n_cost else "#888")
        self.lbl_cost_status.pack(side=tk.LEFT)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        f1 = tk.LabelFrame(inner, text=" 📅 模块1：定时自动调价（已上架）",
                           font=self.FONT_BOLD, fg="#389e0d", padx=10, pady=8)
        f1.pack(fill=tk.X, **PAD)

        r1 = tk.Frame(f1); r1.pack(fill=tk.X, pady=2)
        self.auto1_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(r1, text="启用", variable=self.auto1_enabled,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Label(r1, text="每天执行时间：", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16,2))
        self.auto1_time = tk.StringVar(value="09:00")
        ttk.Entry(r1, textvariable=self.auto1_time, width=7,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Label(r1, text="（格式 HH:MM）", font=("PingFang SC",11), fg="#888").pack(side=tk.LEFT, padx=4)

        r1b = tk.Frame(f1); r1b.pack(fill=tk.X, pady=2)
        self.auto1_only_lower = tk.BooleanVar(value=True)
        tk.Checkbutton(r1b, text="只降价不涨价", variable=self.auto1_only_lower,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.auto1_no_data_skip = tk.BooleanVar(value=True)
        tk.Checkbutton(r1b, text="无历史数据跳过", variable=self.auto1_no_data_skip,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=16)

        r1c = tk.Frame(f1); r1c.pack(fill=tk.X, pady=(4,2))
        tk.Button(r1c, text="▶ 立即执行一次", font=self.FONT_DEFAULT,
                  bg="#52c41a", fg="black",
                  command=lambda: self._run_with_log(self._auto_run_reprice)
                  ).pack(side=tk.LEFT, padx=(0,8))
        tk.Label(r1c, text="对「批量调价」页已导入的在架商品匹配建议价并改价",
                 font=("PingFang SC",11), fg="#888").pack(side=tk.LEFT)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 模块 2：滞销预警 + 自动降阶
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        f2 = tk.LabelFrame(inner, text=" ⏳ 模块2：滞销预警 + 自动降阶",
                           font=self.FONT_BOLD, fg="#d46b08", padx=10, pady=8)
        f2.pack(fill=tk.X, **PAD)

        r2a = tk.Frame(f2); r2a.pack(fill=tk.X, pady=2)
        self.auto2_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(r2a, text="启用", variable=self.auto2_enabled,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Label(r2a, text="检查周期：每隔",
                 font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16,2))
        self.auto2_interval_h = tk.StringVar(value="6")
        ttk.Entry(r2a, textvariable=self.auto2_interval_h, width=4,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Label(r2a, text="小时", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)

        r2b = tk.Frame(f2); r2b.pack(fill=tk.X, pady=2)
        tk.Label(r2b, text="第一阶：超过", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.auto2_days1 = tk.StringVar(value="3")
        ttk.Entry(r2b, textvariable=self.auto2_days1, width=4,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        tk.Label(r2b, text="天未售 → ", font=self.FONT_DEFAULT).pack(side=tk.LEFT)

        # 第一阶段降价策略选择
        self.auto2_stage1_strategy = tk.StringVar(value="conservative")
        strategies = [
            ("降至保守动销价", "conservative"),
            ("降价百分比", "percent"),
            ("降价固定金额", "fixed")
        ]
        for i, (label, value) in enumerate(strategies):
            tk.Radiobutton(r2b, text=label, variable=self.auto2_stage1_strategy,
                          value=value, font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)

        # 第一阶段自定义参数
        r2b2 = tk.Frame(f2); r2b2.pack(fill=tk.X, pady=2)
        tk.Label(r2b2, text="    └ 百分比：降", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.auto2_stage1_pct = tk.StringVar(value="5")
        ttk.Entry(r2b2, textvariable=self.auto2_stage1_pct, width=4,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        tk.Label(r2b2, text="% 或 固定金额：降", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(2,2))
        self.auto2_stage1_amt = tk.StringVar(value="100")
        ttk.Entry(r2b2, textvariable=self.auto2_stage1_amt, width=6,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        tk.Label(r2b2, text="元", font=self.FONT_DEFAULT).pack(side=tk.LEFT)

        r2c = tk.Frame(f2); r2c.pack(fill=tk.X, pady=2)
        tk.Label(r2c, text="第二阶：超过", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.auto2_days2 = tk.StringVar(value="7")
        ttk.Entry(r2c, textvariable=self.auto2_days2, width=4,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        tk.Label(r2c, text="天未售 → 在第一阶价格基础上再降",
                 font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        self.auto2_extra_pct = tk.StringVar(value="1")
        ttk.Entry(r2c, textvariable=self.auto2_extra_pct, width=4,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        tk.Label(r2c, text="%", font=self.FONT_DEFAULT).pack(side=tk.LEFT)

        r2c2 = tk.Frame(f2); r2c2.pack(fill=tk.X, pady=2)
        self.auto2_use_fixed = tk.BooleanVar(value=False)
        tk.Checkbutton(r2c2, text="或改为固定降价：",
                       variable=self.auto2_use_fixed,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.auto2_extra_amt = tk.StringVar(value="100")
        ttk.Entry(r2c2, textvariable=self.auto2_extra_amt, width=6,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        tk.Label(r2c2, text="元（勾选后忽略上方百分比）",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=4)

        r2d = tk.Frame(f2); r2d.pack(fill=tk.X, pady=2)
        self.auto2_alert_only = tk.BooleanVar(value=False)
        tk.Checkbutton(r2d, text="仅预警不自动改价（只打日志）",
                       variable=self.auto2_alert_only,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)

        r2e = tk.Frame(f2); r2e.pack(fill=tk.X, pady=(4,2))
        tk.Button(r2e, text="🔍 预览降价方案", font=self.FONT_DEFAULT,
                  bg="#1890ff", fg="white",
                  command=lambda: threading.Thread(
                      target=self._preview_stale_reprice, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=(0,8))
        tk.Button(r2e, text="▶ 立即检查一次", font=self.FONT_DEFAULT,
                  bg="#fa8c16", fg="black",
                  command=lambda: self._run_with_log(self._auto_run_stale)
                  ).pack(side=tk.LEFT, padx=(0,8))
        tk.Label(r2e, text="对「批量调价」页已导入的在架商品检查库龄，按阈值降价或预警",
                 font=("PingFang SC",11), fg="#888").pack(side=tk.LEFT)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 模块 3：未上架自动定价上架
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        f3 = tk.LabelFrame(inner, text=" 🚀 模块3：未上架商品自动定价上架",
                           font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=8)
        f3.pack(fill=tk.X, **PAD)

        r3a = tk.Frame(f3); r3a.pack(fill=tk.X, pady=2)
        self.auto3_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(r3a, text="启用", variable=self.auto3_enabled,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Label(r3a, text="扫描间隔：每隔",
                 font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16,2))
        self.auto3_interval_m = tk.StringVar(value="30")
        ttk.Entry(r3a, textvariable=self.auto3_interval_m, width=4,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)
        tk.Label(r3a, text="分钟扫描一次", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=2)

        r3b = tk.Frame(f3); r3b.pack(fill=tk.X, pady=2)
        self.auto3_no_data_skip = tk.BooleanVar(value=True)
        tk.Checkbutton(r3b, text="无历史数据时跳过（不定价）",
                       variable=self.auto3_no_data_skip,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)

        r3c = tk.Frame(f3); r3c.pack(fill=tk.X, pady=(4,2))
        tk.Button(r3c, text="▶ 立即扫描一次", font=self.FONT_DEFAULT,
                  bg="#1890ff", fg="black",
                  command=lambda: self._run_with_log(self._auto_run_list_new)
                  ).pack(side=tk.LEFT, padx=(0,8))
        tk.Label(r3c, text="对「批量调价」页已导入的未上架商品匹配历史成交价并定价上架",
                 font=("PingFang SC",11), fg="#888").pack(side=tk.LEFT)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 模块 4：定时销售播报 → 企业微信机器人
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        f4 = tk.LabelFrame(inner, text=" 🔔 模块4：定时销售播报（企业微信机器人）",
                           font=self.FONT_BOLD, fg="#cf1322", padx=10, pady=8)
        f4.pack(fill=tk.X, **PAD)

        # Webhook URL
        r4a = tk.Frame(f4); r4a.pack(fill=tk.X, pady=2)
        self.auto4_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(r4a, text="启用", variable=self.auto4_enabled,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Label(r4a, text="企业微信 Webhook URL：",
                 font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16, 2))
        self.auto4_webhook = tk.StringVar()
        ttk.Entry(r4a, textvariable=self.auto4_webhook, width=42,
                  font=("Menlo", 11)).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(r4a, text="💾 保存", font=self.FONT_DEFAULT, bg="#52c41a", fg="black",
                  command=self._save_webhook_config).pack(side=tk.LEFT, padx=2)
        tk.Button(r4a, text="🧪 测试", font=self.FONT_DEFAULT,
                  command=lambda: threading.Thread(
                      target=self._auto4_test_webhook, daemon=True).start()
                  ).pack(side=tk.LEFT)

        # 播报时间
        r4b = tk.Frame(f4); r4b.pack(fill=tk.X, pady=2)
        tk.Label(r4b, text="播报时间（每天，可多个，逗号分隔）：",
                 font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.auto4_times = tk.StringVar(value="09:00,12:00,18:00,21:00")
        ttk.Entry(r4b, textvariable=self.auto4_times, width=28,
                  font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(6, 0))

        # 播报内容选项
        r4c = tk.Frame(f4); r4c.pack(fill=tk.X, pady=2)
        self.auto4_inc_on_sale  = tk.BooleanVar(value=True)
        self.auto4_inc_sold    = tk.BooleanVar(value=True)
        self.auto4_inc_stale   = tk.BooleanVar(value=True)
        self.auto4_inc_unlisted = tk.BooleanVar(value=True)
        tk.Checkbutton(r4c, text="已上架数量",  variable=self.auto4_inc_on_sale,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Checkbutton(r4c, text="今日已售",    variable=self.auto4_inc_sold,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=12)
        tk.Checkbutton(r4c, text="滞销预警",    variable=self.auto4_inc_stale,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=12)
        tk.Checkbutton(r4c, text="待定价商品", variable=self.auto4_inc_unlisted,
                       font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=12)

        r4d = tk.Frame(f4); r4d.pack(fill=tk.X, pady=(4, 2))
        tk.Button(r4d, text="▶ 立即播报一次", font=self.FONT_DEFAULT,
                  bg="#cf1322", fg="white",
                  command=lambda: threading.Thread(
                      target=self._auto_run_feishu_report, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(r4d, text="拉取已售/在架/未上架数据，汇总后推送到飞书群",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 模块 5：企业微信自建应用双向对话
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 智能调价模块：整合模块1/2/3
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        f_smart = tk.LabelFrame(inner, text=" 🎯 智能调价（整合模块1/2/3）",
                               font=self.FONT_BOLD, fg="#722ed1", padx=10, pady=8)
        f_smart.pack(fill=tk.X, **PAD)

        r_smart1 = tk.Frame(f_smart); r_smart1.pack(fill=tk.X, pady=2)
        self.auto_smart_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(r_smart1, text="启用智能调价", variable=self.auto_smart_enabled,
                      font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        tk.Label(r_smart1, text="每天", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16,4))
        self.auto_smart_time = tk.StringVar(value="09:00")
        ttk.Entry(r_smart1, textvariable=self.auto_smart_time,
                 font=self.FONT_DEFAULT, width=6).pack(side=tk.LEFT)
        tk.Label(r_smart1, text="自动扫描并调价", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=4)

        r_smart2 = tk.Frame(f_smart); r_smart2.pack(fill=tk.X, pady=2)
        self.auto_smart_auto_confirm = tk.BooleanVar(value=False)
        tk.Checkbutton(r_smart2, text="自动确认调价（无需人工审核）",
                      variable=self.auto_smart_auto_confirm,
                      font=self.FONT_DEFAULT, fg="#ff4d4f").pack(side=tk.LEFT)
        tk.Label(r_smart2, text="⚠️ 开启后将自动执行所有调价操作",
                font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=8)

        r_smart3 = tk.Frame(f_smart); r_smart3.pack(fill=tk.X, pady=2)
        tk.Button(r_smart3, text="🎯 立即运行智能调价", font=self.FONT_DEFAULT,
                 bg="#722ed1", fg="white",
                 command=lambda: threading.Thread(
                     target=self._auto_run_smart_reprice, daemon=True).start()
                 ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(r_smart3, text="扫描需要调价的商品并执行调价",
                font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT)

        f5 = tk.LabelFrame(inner, text=" 💬 模块5：企业微信自建应用（双向指令）",
                           font=self.FONT_BOLD, fg="#0050b3", padx=10, pady=8)
        f5.pack(fill=tk.X, **PAD)

        # 配置行 A：CorpID
        r5a = tk.Frame(f5); r5a.pack(fill=tk.X, pady=2)
        tk.Label(r5a, text="企业ID(CorpID)：", font=self.FONT_DEFAULT, width=16, anchor="e").pack(side=tk.LEFT)
        self.wx5_corpid = tk.StringVar(value=self.wxapp_config.corp_id)
        ttk.Entry(r5a, textvariable=self.wx5_corpid, font=self.FONT_DEFAULT, width=28).pack(side=tk.LEFT, padx=4)

        # 配置行 B：AgentID + Secret
        r5b = tk.Frame(f5); r5b.pack(fill=tk.X, pady=2)
        tk.Label(r5b, text="AgentID：", font=self.FONT_DEFAULT, width=16, anchor="e").pack(side=tk.LEFT)
        self.wx5_agentid = tk.StringVar(value=self.wxapp_config.agent_id)
        ttk.Entry(r5b, textvariable=self.wx5_agentid, font=self.FONT_DEFAULT, width=12).pack(side=tk.LEFT, padx=4)
        tk.Label(r5b, text="Secret：", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16,2))
        self.wx5_secret = tk.StringVar(value=self.wxapp_config.secret)
        ent5s = ttk.Entry(r5b, textvariable=self.wx5_secret, font=self.FONT_DEFAULT, width=36, show="*")
        ent5s.pack(side=tk.LEFT, padx=4)
        tk.Button(r5b, text="👁", font=self.FONT_DEFAULT, width=2,
                  command=lambda e=ent5s: e.config(show="" if e.cget("show")=="*" else "*")
                  ).pack(side=tk.LEFT)

        # 配置行 C：回调 Token + AESKey
        r5c = tk.Frame(f5); r5c.pack(fill=tk.X, pady=2)
        tk.Label(r5c, text="回调Token：", font=self.FONT_DEFAULT, width=16, anchor="e").pack(side=tk.LEFT)
        self.wx5_token = tk.StringVar(value=self.wxapp_config.token)
        ttk.Entry(r5c, textvariable=self.wx5_token, font=self.FONT_DEFAULT, width=18).pack(side=tk.LEFT, padx=4)
        tk.Label(r5c, text="AESKey：", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(16,2))
        self.wx5_aeskey = tk.StringVar(value=self.wxapp_config.aes_key)
        ttk.Entry(r5c, textvariable=self.wx5_aeskey, font=self.FONT_DEFAULT, width=46).pack(side=tk.LEFT, padx=4)

        # 配置行 D：ngrok authtoken（pyngrok自动管理隧道）
        r5d = tk.Frame(f5); r5d.pack(fill=tk.X, pady=2)
        tk.Label(r5d, text="ngrok Token：", font=self.FONT_DEFAULT, width=16, anchor="e").pack(side=tk.LEFT)
        self.wx5_ngrok_token = tk.StringVar(value=self.wxapp_config.ngrok_authtoken)
        ent_ngrok = ttk.Entry(r5d, textvariable=self.wx5_ngrok_token,
                              font=("Menlo", 11), width=40, show="*")
        ent_ngrok.pack(side=tk.LEFT, padx=4)
        tk.Button(r5d, text="👁", font=self.FONT_DEFAULT, width=2,
                  command=lambda e=ent_ngrok: e.config(show="" if e.cget("show")=="*" else "*")
                  ).pack(side=tk.LEFT, padx=2)
        tk.Label(r5d, text="免费注册 ngrok.com 拿 Token，程序自动管理隧道无需手动操作",
                 font=("PingFang SC", 10), fg="#888").pack(side=tk.LEFT, padx=6)

        # 配置行 E：当前公网URL（只读，自动生成）
        r5e0 = tk.Frame(f5); r5e0.pack(fill=tk.X, pady=(0, 4))
        tk.Label(r5e0, text="回调URL（自动）：", font=self.FONT_DEFAULT, width=16, anchor="e").pack(side=tk.LEFT)
        self.wx5_callback_url = tk.StringVar(value="（启动后自动生成，粘贴到企业微信后台）")
        url_entry = ttk.Entry(r5e0, textvariable=self.wx5_callback_url,
                              font=("Menlo", 10), width=48, state="readonly")
        url_entry.pack(side=tk.LEFT, padx=4)
        tk.Button(r5e0, text="📋", font=self.FONT_DEFAULT, width=2,
                  command=lambda: (self.root.clipboard_clear(),
                                   self.root.clipboard_append(self.wx5_callback_url.get()))
                  ).pack(side=tk.LEFT, padx=2)

        # 操作行
        r5e = tk.Frame(f5); r5e.pack(fill=tk.X, pady=(2, 2))
        tk.Button(r5e, text="💾 保存配置", font=self.FONT_DEFAULT,
                  bg="#1890ff", fg="black",
                  command=self._wxapp_save_config).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(r5e, text="🔍 验证连接", font=self.FONT_DEFAULT,
                  command=lambda: threading.Thread(target=self._wxapp_test, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=(0, 8))
        self.btn_wxapp_server = tk.Button(
            r5e, text="▶ 一键启动", font=self.FONT_DEFAULT,
            bg="#52c41a", fg="black",
            command=self._wxapp_toggle_server)
        self.btn_wxapp_server.pack(side=tk.LEFT, padx=(0, 8))
        self.lbl_wxapp_status = tk.Label(r5e, text="⚪ 未启动", font=("PingFang SC", 11), fg="#888")
        self.lbl_wxapp_status.pack(side=tk.LEFT, padx=8)

        # 可用指令说明行
        r5f = tk.Frame(f5); r5f.pack(fill=tk.X, pady=(4, 0))
        tk.Label(r5f,
                 text="可用指令：今日销售  库存查询 iPhone15Pro  触发调价  滞销预警  帮助",
                 font=("PingFang SC", 11), fg="#555").pack(side=tk.LEFT)

        # ── 全局调度开关 ────────────────────────────────────────────────────
        frm_start = tk.Frame(inner); frm_start.pack(fill=tk.X, **PAD)
        self.btn_auto_start = tk.Button(
            frm_start, text="▶ 启动自动化调度器",
            font=self.FONT_BOLD, bg="#52c41a", fg="black", width=22,
            command=self._toggle_auto_scheduler,
        )
        self.btn_auto_start.pack(side=tk.LEFT, padx=(0,12))
        tk.Label(frm_start, text="启动后后台持续运行，关闭窗口自动停止",
                 font=("PingFang SC",11), fg="#888").pack(side=tk.LEFT)

        # 隐藏的 Text 用于存日志内容（不显示在 tab 内）
        self.auto_log = tk.Text(self.root)   # 不 pack，只用作缓冲

        self._refresh_auto_account_list()

    # ── 自动化：账号刷新 ──────────────────────────────────────────────────────
    # ── ERP：以下为完整实现版本（保留在下方）──────────────────────────────────
        self._load_webhook_config()  # 加载企业微信 Webhook 配置

    def _erp_sync_and_import_placeholder(self):
        pass  # 此占位方法不会被调用，真实逻辑在下方完整版本中

        fetcher = ErpFetcher(token, version)
        all_raw: list[dict] = []

        def _status(msg: str):
            self._auto_log(msg)
            self.root.after(0, lambda m=msg: self.lbl_erp_sync_status.config(
                text=m, fg="#1890ff"))

        _status("🔄 开始从爱管机 ERP 拉取数据...")

        # ── 已上架 ──────────────────────────────────────────────────────────
        if self.erp_sync_shelf.get():
            _status("  拉取已上架商品...")
            try:
                items = fetcher.fetch_put_shelf(on_log=self._auto_log)
                self._auto_log(f"  ✅ 已上架：{len(items)} 件")
                all_raw.extend(items)
            except Exception as e:
                self._auto_log(f"  ❌ 已上架拉取失败（{type(e).__name__}）：{e}")
                self._auto_log("  💡 请点「🔬 调试」按钮查看接口原始响应")

        # ── 在库库存 ────────────────────────────────────────────────────────
        if self.erp_sync_stock.get():
            _status("  拉取在库库存...")
            try:
                items = fetcher.fetch_stock(on_log=self._auto_log)
                self._auto_log(f"  ✅ 在库库存：{len(items)} 件")
                all_raw.extend(items)
            except Exception as e:
                self._auto_log(f"  ❌ 在库库存拉取失败（{type(e).__name__}）：{e}")

        # ── 已售订单 ────────────────────────────────────────────────────────
        if self.erp_sync_sold.get():
            _status("  拉取已售订单...")
            try:
                items = fetcher.fetch_sold(on_log=self._auto_log)
                self._auto_log(f"  ✅ 已售记录：{len(items)} 件")
                all_raw.extend(items)
            except Exception as e:
                self._auto_log(f"  ❌ 已售记录拉取失败（{type(e).__name__}）：{e}")

        if not all_raw:
            _status("⚠️ 未拉取到任何数据，请检查 Token 或勾选项")
            return

        # ── 提取质检码并查询转转状态导入 ────────────────────────────────────
        codes = ErpFetcher.extract_codes(all_raw)
        self._auto_log(f"📋 共提取 {len(codes)} 个质检码/IMEI，开始查询转转状态...")
        _status(f"查询转转状态中（共{len(codes)}个）...")

        cookie = self._get_auto_cookie()
        if not cookie:
            # 没有选择转转店铺时，只记录质检码到导入窗口输入框，让用户手动查
            self._auto_log("⚠️ 未选择转转操作店铺，已将质检码填入商品管理窗口，请手动点「查询并导入」")
            self.root.after(0, lambda: self._erp_fill_codes_to_import_box(codes))
            _status(f"✅ 已填入 {len(codes)} 个质检码到商品管理窗口")
            return

        from_svc = __import__("requests")   # 确保 requests 已导入
        svc = ImeiService(cookie)
        existing_ids = {i.product_id for i in self._batch_items}
        added = skip_dup = not_found_count = 0
        batch_size = 50

        for i in range(0, len(codes), batch_size):
            batch = codes[i: i + batch_size]
            _status(f"查询转转状态 {i+1}~{min(i+batch_size, len(codes))}/{len(codes)}...")
            try:
                found, not_found = svc.fetch_by_codes(batch)
            except Exception as e:
                self._auto_log(f"  ⚠️ 第{i//batch_size+1}批查询失败：{e}，跳过")
                continue

            not_found_count += len(not_found)
            for r in found:
                item = self._parse_raw_to_batch_item(r)
                if self.engine:
                    res = self.engine.query(item.model, item.condition,
                                            item.capacity, item.color)
                    if res: item.suggested_price = res.suggested_price
                if item.product_id in existing_ids:
                    skip_dup += 1
                else:
                    self._batch_items.append(item)
                    existing_ids.add(item.product_id)
                    added += 1
                    self._auto_log(
                        f"  ✅ [{item.qc_code or item.imei}]  "
                        f"{item.model} {item.condition} {item.capacity} {item.color}  "
                        f"状态:{item.status_name}(code={item.status_code!r})  "
                        f"当前¥{int(item.current_price)}"
                    )

        self.root.after(0, self._refresh_import_tree)
        summary = (f"✅ ERP同步完成：新增 {added} 件，"
                   f"跳过重复 {skip_dup} 件，转转未找到 {not_found_count} 件")
        self._auto_log(summary)
        _status(summary)

    def _erp_debug(self):
        """调试：发一次原始请求，把完整响应打到日志，帮助排查数据结构"""
        token = self.erp_config.token or self.erp_token_var.get().strip()
        if not token:
            self._auto_log("❌ 调试：请先填写 Token"); return

        self._auto_log("🔬 调试请求中……")
        headers = {
            "Authorization": token,
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/plain, */*",
            "User-Agent":    USER_AGENT,
            "Origin":        "https://saas.aiguanji.com",
            "Referer":       "https://saas.aiguanji.com/",
        }
        payload = {"page": 1, "limit": 5,
                   "value1": "", "cate_id": "", "brand_id": "",
                   "product_id": "", "sale_channel_id": "", "selected_attr": {}}
        try:
            resp = requests.post(ERP_PUT_SHELF_URL, json=payload,
                                 headers=headers, timeout=15)
            self._auto_log(f"  HTTP状态码: {resp.status_code}")
            try:
                body = resp.json()
            except Exception:
                self._auto_log(f"  ⚠️ 响应不是JSON，原始内容：{resp.text[:300]}")
                return

            self._auto_log(f"  code字段: {body.get('code')!r}")
            self._auto_log(f"  msg字段:  {body.get('msg')!r}")
            data = body.get("data")
            self._auto_log(f"  data类型: {type(data).__name__}")
            if isinstance(data, dict):
                self._auto_log(f"  data的key: {list(data.keys())}")
                inner = data.get("data")
                self._auto_log(f"  data.data类型: {type(inner).__name__}  "
                               f"长度: {len(inner) if isinstance(inner, list) else '—'}")
                if isinstance(inner, list) and inner:
                    first = inner[0]
                    self._auto_log(f"  第一条的key: {list(first.keys())[:15]}")
                    self._auto_log(f"  sale_no={first.get('sale_no')!r}  "
                                   f"imei={first.get('imei')!r}  "
                                   f"name={first.get('name')!r}")
                elif isinstance(inner, list) and not inner:
                    self._auto_log("  ⚠️ data.data 是空列表 []")
                else:
                    self._auto_log(f"  ⚠️ data.data 结构异常: {str(inner)[:200]}")
            elif isinstance(data, list):
                self._auto_log(f"  data直接是列表，长度: {len(data)}")
                if data:
                    self._auto_log(f"  第一条的key: {list(data[0].keys())[:15]}")
            else:
                self._auto_log(f"  ⚠️ data结构异常: {str(data)[:300]}")
        except Exception as e:
            self._auto_log(f"  ❌ 请求异常：{e}")

    def _erp_sync_cost_prices(self):
        """从 ERP 拉取在库+已上架的成本价，计算+2%税后存为本地缓存，用于底价校验"""
        token   = self.erp_config.token or self.erp_token_var.get().strip()
        version = self.erp_version_var.get().strip() or self.erp_config.version
        if not token:
            self._auto_log("❌ 成本底价同步：请先在模块0填写并保存 ERP Token"); return

        self._auto_log("💰 开始同步 ERP 成本底价...")
        try:
            fetcher  = ErpFetcher(token, version)
            cost_map = fetcher.fetch_cost_prices(on_log=self._auto_log)
            if not cost_map:
                self._auto_log("⚠️ 未拉取到成本价数据，请确认 ERP 字段名（cost_price）是否正确")
                return
            self.cost_price_map.save(cost_map)
            msg = f"✅ 成本底价同步完成，共 {len(cost_map)} 个SKU（已含2%税保本线）"
            self._auto_log(msg)
            # 更新 UI 状态标签
            self.root.after(0, lambda: self.lbl_cost_status.config(
                text=f"📦 已缓存 {len(cost_map)} 个SKU成本底价",
                fg="#389e0d"
            ))
            # 如果批量列表已有数据，重新刷新底价列
            if self._batch_items:
                self.root.after(0, self._refresh_batch_cost_floor)
        except Exception as e:
            self._auto_log(f"❌ 成本底价同步失败：{e}")

    def _open_cost_price_viewer(self):
        """浮窗：展示本地缓存的成本底价明细表，支持搜索过滤"""
        if self.cost_price_map.is_empty:
            messagebox.showinfo("提示", "尚未同步成本底价，请先点击「💰 同步成本底价」")
            return

        win = tk.Toplevel(self.root)
        win.title("成本底价明细")
        win.geometry("660x500")
        win.lift()

        # ── 搜索栏 ────────────────────────────────────────────────────────────
        top = tk.Frame(win, pady=6, padx=10)
        top.pack(fill=tk.X)
        tk.Label(top, text="搜索：", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=search_var, font=self.FONT_DEFAULT, width=22)
        ent.pack(side=tk.LEFT, padx=6)
        tk.Label(top, text="（型号/颜色/内存任意关键字）",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT)

        # ── 表格 ─────────────────────────────────────────────────────────────
        cols = ("model", "color", "memory", "cost_floor")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=20)
        tree.heading("model",       text="型号");       tree.column("model",       width=200, anchor="w")
        tree.heading("color",       text="颜色");       tree.column("color",       width=100, anchor="center")
        tree.heading("memory",      text="内存");       tree.column("memory",      width=90,  anchor="center")
        tree.heading("cost_floor",  text="成本底价（含2%税）"); tree.column("cost_floor", width=160, anchor="center")

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=6)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, pady=6, padx=(0, 6))

        # 底部统计行
        lbl_count = tk.Label(win, text="", font=("PingFang SC", 11), fg="#888")
        lbl_count.pack(side=tk.BOTTOM, pady=4)

        # ── 填充数据 ──────────────────────────────────────────────────────────
        all_rows = []
        for key, floor_val in sorted(self.cost_price_map._map.items()):
            parts = key.split("|")
            model  = parts[0] if len(parts) > 0 else ""
            color  = parts[1] if len(parts) > 1 else ""
            memory = parts[2] if len(parts) > 2 else ""
            all_rows.append((model, color, memory, floor_val))

        def _render(kw=""):
            for row in tree.get_children():
                tree.delete(row)
            kw = kw.strip().lower()
            shown = 0
            for model, color, memory, floor_val in all_rows:
                if kw and kw not in f"{model}{color}{memory}".lower():
                    continue
                tree.insert("", tk.END, values=(
                    model, color, memory, f"¥{floor_val:.0f}"
                ))
                shown += 1
            lbl_count.config(
                text=f"显示 {shown} / 共 {len(all_rows)} 个SKU"
            )

        _render()
        search_var.trace_add("write", lambda *_: _render(search_var.get()))
        ent.focus_set()

    def _refresh_batch_cost_floor(self):
        """重新用最新成本底价表刷新批量列表的 cost_floor 字段并重渲染"""
        for item in self._batch_items:
            floor = self.cost_price_map.get_fuzzy(item.model, item.color, item.capacity)
            item.cost_floor = int(floor) if floor else 0
        self._render_batch_tree()
        self._batch_log("💰 已用最新成本底价刷新底价列")

    def _erp_fill_codes_to_import_box(self, codes: list[str]):
        """没有转转 cookie 时，把质检码直接填到商品管理窗口输入框"""
        self._open_import_window()
        if hasattr(self, "_imp_code_box") and self._imp_code_box.winfo_exists():
            self._imp_code_box.delete("1.0", tk.END)
            self._imp_code_box.insert("1.0", "\n".join(codes))

    def _refresh_auto_account_list(self):
        names = [a.name for a in self.account_mgr.accounts]
        self.combo_auto_account["values"] = names
        if names and not self.combo_auto_account.get():
            self.combo_auto_account.set(names[0])

    def _get_auto_cookie(self) -> Optional[str]:
        name = self.combo_auto_account.get()
        for a in self.account_mgr.accounts:
            if a.name == name:
                return a.cookie
        return None


    # ════════════════════════════════════════════════════════════════════════
    # 爱管机 ERP 对接方法
    # ════════════════════════════════════════════════════════════════════════
    def _erp_save_token(self):
        """保存 ERP Authorization token + Version"""
        token = self.erp_token_var.get().strip()
        if not token:
            messagebox.showwarning("提示", "请先填写 Authorization Token")
            return
        version = self.erp_version_var.get().strip()
        self.erp_config.save(token, version)
        self.root.after(0, lambda: self.lbl_erp_token_status.config(
            text="✅ 已保存", fg="#389e0d"))
        self._auto_log(f"💾 爱管机 Token 已保存，Version={version or '（未填）'}")

    def _erp_check_token(self):
        """验证 ERP token 是否有效"""
        token   = self.erp_token_var.get().strip()
        version = self.erp_version_var.get().strip()
        if not token:
            self.root.after(0, lambda: self.lbl_erp_token_status.config(
                text="❌ 请先填写 Token", fg="#cf1322"))
            return
        self.root.after(0, lambda: self.lbl_erp_token_status.config(
            text="🔍 验证中...", fg="#1890ff"))
        ok, msg = ErpFetcher(token, version).check_token()
        color = "#389e0d" if ok else "#cf1322"
        icon  = "✅" if ok else "❌"
        self.root.after(0, lambda: self.lbl_erp_token_status.config(
            text=f"{icon} {msg}", fg=color))
        self._auto_log(f"{'✅' if ok else '❌'} 爱管机 Token 验证：{msg}")

    def _erp_sync_and_import(self):
        """
        从爱管机 ERP 拉取商品，提取质检码/IMEI，
        再调用转转接口查询状态，导入到 _batch_items。
        """
        token   = self.erp_token_var.get().strip()
        version = self.erp_version_var.get().strip()
        if not token:
            self._auto_log("❌ ERP同步：请先填写并保存 Authorization Token"); return

        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ ERP同步：请先在上方选择操作店铺"); return

        self.root.after(0, lambda: self.lbl_erp_sync_status.config(
            text="⏳ 同步中...", fg="#1890ff"))
        self._auto_log("🔗 开始从爱管机 ERP 拉取商品...")

        fetcher = ErpFetcher(token, version)
        all_erp_items: list[dict] = []

        # ── 按勾选拉取 ────────────────────────────────────────────────────
        if self.erp_sync_shelf.get():
            self._auto_log("  📋 拉取已上架商品...")
            try:
                items = fetcher.fetch_put_shelf(on_log=self._auto_log)
                self._auto_log(f"  ✅ 已上架：{len(items)} 条")
                all_erp_items.extend(items)
            except Exception as e:
                self._auto_log(f"  ❌ 已上架拉取失败：{e}")

        if self.erp_sync_stock.get():
            self._auto_log("  📦 拉取在库库存...")
            try:
                items = fetcher.fetch_stock(on_log=self._auto_log)
                self._auto_log(f"  ✅ 在库库存：{len(items)} 条")
                all_erp_items.extend(items)
            except Exception as e:
                self._auto_log(f"  ❌ 在库拉取失败：{e}")

        if self.erp_sync_sold.get():
            self._auto_log("  🧾 拉取已售记录...")
            try:
                items = fetcher.fetch_sold(on_log=self._auto_log)
                self._auto_log(f"  ✅ 已售记录：{len(items)} 条")
                all_erp_items.extend(items)
            except Exception as e:
                self._auto_log(f"  ❌ 已售拉取失败：{e}")

        if not all_erp_items:
            self._auto_log("⚠️ ERP同步：未拉取到任何数据，请检查 Token 或勾选同步项")
            self.root.after(0, lambda: self.lbl_erp_sync_status.config(
                text="⚠️ 无数据", fg="#faad14"))
            return

        # ── 提取质检码 / IMEI ────────────────────────────────────────────
        codes = ErpFetcher.extract_codes(all_erp_items)
        self._auto_log(f"📋 从 ERP 提取到 {len(codes)} 个质检码/IMEI，开始查询转转状态...")

        # ── 调用转转接口查询状态，批量导入 ───────────────────────────────
        svc = ImeiService(cookie)
        batch_size = 50
        existing_ids = {i.product_id for i in self._batch_items}
        added = skipped = not_found_count = 0

        for i in range(0, len(codes), batch_size):
            batch = codes[i: i + batch_size]
            self._auto_log(f"  查询第 {i//batch_size + 1} 批（{len(batch)} 个）...")
            try:
                found, not_found = svc.fetch_by_codes(batch)
            except Exception as e:
                self._auto_log(f"  ⚠️ 第{i//batch_size+1}批查询失败：{e}")
                continue

            not_found_count += len(not_found)

            for r in found:
                item = self._parse_raw_to_batch_item(r)
                if self.engine:
                    res = self.engine.query(item.model, item.condition,
                                            item.capacity, item.color)
                    if res:
                        item.suggested_price = res.suggested_price

                if item.product_id in existing_ids:
                    skipped += 1
                    continue

                self._batch_items.append(item)
                existing_ids.add(item.product_id)
                added += 1

                sc = str(item.status_code)
                status_str = ("已上架" if sc == "0" else
                              "未上架" if sc == "60" else
                              "已售"   if sc == "80" else item.status_name)
                sug = f"  建议¥{item.suggested_price}" if item.suggested_price else ""
                self._auto_log(
                    f"  ✅ [{item.qc_code or item.imei}]  "
                    f"{item.model} {item.condition} {item.capacity} {item.color}  "
                    f"状态:{status_str}  当前¥{int(item.current_price)}{sug}"
                )

        # ── 完成 ─────────────────────────────────────────────────────────
        summary = (f"ERP同步完成：新增 {added} 件，跳过重复 {skipped} 件"
                   + (f"，转转未找到 {not_found_count} 件" if not_found_count else ""))
        self._auto_log(f"✅ {summary}")
        self.root.after(0, lambda: (
            self.lbl_erp_sync_status.config(text=f"✅ {summary}", fg="#389e0d"),
            self._update_import_status_label(),
        ))
        # 刷新商品管理窗口（如果已打开）
        self.root.after(0, self._refresh_import_tree)

    # ════════════════════════════════════════════════════════════════════════
    # 商品管理浮窗
    # ════════════════════════════════════════════════════════════════════════
    def _open_import_window(self):
        """打开独立商品管理浮窗（单例，已存在则置顶）"""
        if hasattr(self, "_import_win") and self._import_win and self._import_win.winfo_exists():
            self._import_win.lift(); return

        win = tk.Toplevel(self.root)
        win.title("📋 商品管理")
        win.geometry("920x680")
        win.resizable(True, True)
        self._import_win = win

        # ── 店铺选择（独立，不依赖自动化 tab）────────────────────────────────
        frm_acc = tk.Frame(win, bg="#f0f0f0", pady=6)
        frm_acc.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(frm_acc, text="操作店铺：", font=self.FONT_DEFAULT, bg="#f0f0f0").pack(side=tk.LEFT)
        self._imp_account_combo = ttk.Combobox(frm_acc, state="readonly",
                                               font=self.FONT_DEFAULT, width=20)
        self._imp_account_combo.pack(side=tk.LEFT, padx=6)
        names = [a.name for a in self.account_mgr.accounts]
        self._imp_account_combo["values"] = names
        # 优先同步自动化 tab 的选择，否则取第一个
        cur = self.combo_auto_account.get() if hasattr(self, "combo_auto_account") else ""
        self._imp_account_combo.set(cur if cur in names else (names[0] if names else ""))
        ttk.Button(frm_acc, text="🔄",
                   command=lambda: self._imp_refresh_accounts()).pack(side=tk.LEFT, padx=2)

        # ── 输入区 ────────────────────────────────────────────────────────
        frm_top = tk.LabelFrame(win, text=" 按质检码 / IMEI 导入",
                                font=self.FONT_BOLD, fg="#d46b08", padx=8, pady=6)
        frm_top.pack(fill=tk.X, padx=10, pady=(6, 4))

        tk.Label(frm_top, text="粘贴质检码或 IMEI（每行一个，支持批量）：",
                 font=self.FONT_DEFAULT).pack(anchor="w")
        frm_inp = tk.Frame(frm_top); frm_inp.pack(fill=tk.X, pady=(4, 0))
        self._imp_code_box = tk.Text(
            frm_inp, height=7, font=("Menlo", 12),
            bg="#fafafa", fg="#111111", insertbackground="#111111",
            relief="solid", bd=1, padx=6, pady=4,
        )
        imp_sb = ttk.Scrollbar(frm_inp, orient="vertical",
                               command=self._imp_code_box.yview)
        self._imp_code_box.configure(yscrollcommand=imp_sb.set)
        self._imp_code_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        imp_sb.pack(side=tk.RIGHT, fill=tk.Y)

        frm_btn = tk.Frame(frm_top); frm_btn.pack(fill=tk.X, pady=(6, 0))
        tk.Button(frm_btn, text="🔍 查询并导入", font=self.FONT_DEFAULT,
                  bg="#52c41a", fg="black",
                  command=lambda: threading.Thread(
                      target=self._do_import_by_codes, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(frm_btn, text="🗑 清空输入", font=self.FONT_DEFAULT,
                  command=lambda: self._imp_code_box.delete("1.0", tk.END)
                  ).pack(side=tk.LEFT, padx=(0, 16))
        tk.Button(frm_btn, text="🔄 刷新状态", font=self.FONT_DEFAULT,
                  bg="#1890ff", fg="black",
                  command=lambda: threading.Thread(
                      target=self._refresh_import_status, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(frm_btn, text="🗑 清空全部", font=self.FONT_DEFAULT,
                  fg="#cf1322", command=self._auto_clear_items).pack(side=tk.LEFT)

        self._imp_status_lbl = tk.Label(frm_btn, text="",
                                        font=("PingFang SC", 11), fg="#888")
        self._imp_status_lbl.pack(side=tk.LEFT, padx=12)

        # ── ERP IMEI 导入行 ───────────────────────────────────────────────
        frm_erp = tk.Frame(frm_top); frm_erp.pack(fill=tk.X, pady=(2, 0))
        tk.Button(frm_erp, text="📥 从 ERP 导入（IMEI1）", font=self.FONT_DEFAULT,
                  bg="#13c2c2", fg="black",
                  command=lambda: threading.Thread(
                      target=self._do_import_from_erp_imei, daemon=True).start()
                  ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(frm_erp,
                 text="从爱管机拉取在库/已上架商品的 IMEI1，自动查询转转在架及未上架状态",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT)

        # ── 定时自动刷新行 ────────────────────────────────────────────────
        frm_auto_ref = tk.Frame(frm_top); frm_auto_ref.pack(fill=tk.X, pady=(4, 2))
        self._imp_auto_refresh_var = tk.BooleanVar(value=False)
        self._imp_auto_refresh_btn = tk.Button(
            frm_auto_ref, text="⏰ 开启定时刷新", font=self.FONT_DEFAULT,
            bg="#722ed1", fg="black",
            command=self._toggle_imp_auto_refresh,
        )
        self._imp_auto_refresh_btn.pack(side=tk.LEFT, padx=(0, 8))

        tk.Label(frm_auto_ref, text="每隔", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self._imp_refresh_interval = tk.StringVar(value="5")
        ttk.Entry(frm_auto_ref, textvariable=self._imp_refresh_interval,
                  width=4, font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=4)
        tk.Label(frm_auto_ref, text="分钟自动刷新一次，有商品状态变化时推送企业微信",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=4)

        self._imp_next_refresh_lbl = tk.Label(frm_auto_ref, text="",
                                              font=("PingFang SC", 11), fg="#1890ff")
        self._imp_next_refresh_lbl.pack(side=tk.LEFT, padx=8)

        # ── 状态筛选栏 ────────────────────────────────────────────────────────
        frm_filter = tk.Frame(win, bg="#f0f0f0", pady=6)
        frm_filter.pack(fill=tk.X, padx=10, pady=(4, 0))
        tk.Label(frm_filter, text="状态筛选：", font=self.FONT_DEFAULT, bg="#f0f0f0").pack(side=tk.LEFT, padx=(0, 6))

        self._imp_status_filters = {}
        for status_name, (fg, bg) in [("已上架", ("#237804", "#f6ffed")),
                                       ("未上架", ("#ad4e00", "#fff7e6")),
                                       ("已售", ("#595959", "#fafafa"))]:
            var = tk.BooleanVar(value=True)
            self._imp_status_filters[status_name] = var
            cb = tk.Checkbutton(
                frm_filter,
                text=status_name,
                variable=var,
                font=("PingFang SC", 12, "bold"),
                fg=fg, bg=bg,
                selectcolor=bg,
                activeforeground=fg,
                activebackground=bg,
                relief="solid", bd=1,
                padx=8, pady=3,
                command=self._refresh_import_tree,
            )
            cb.pack(side=tk.LEFT, padx=4, pady=2)

        # ── 商品列表 ──────────────────────────────────────────────────────
        frm_tree = tk.Frame(win); frm_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))
        cols = ("qc", "model", "condition", "capacity", "color", "status", "price", "suggested")

        # 设置字体更大、行高更高的样式
        style = ttk.Style()
        style.configure("Imp.Treeview",
                        font=("PingFang SC", 13),
                        rowheight=26,
                        background="#ffffff",
                        foreground="#222222",
                        fieldbackground="#ffffff")
        style.configure("Imp.Treeview.Heading",
                        font=("PingFang SC", 13, "bold"))
        style.map("Imp.Treeview", background=[("selected", "#bae0ff")])

        self._imp_tree = ttk.Treeview(frm_tree, columns=cols, show="headings",
                                      height=20, style="Imp.Treeview")
        hdrs = [("qc","质检码",130), ("model","型号",180), ("condition","成色",100),
                ("capacity","容量",75), ("color","颜色",75), ("status","状态",80),
                ("price","当前价",80), ("suggested","建议价",80)]
        for col, text, w in hdrs:
            self._imp_tree.heading(col, text=text)
            self._imp_tree.column(col, width=w, anchor="center")
        self._imp_tree.column("model", anchor="w")
        vsb = ttk.Scrollbar(frm_tree, orient="vertical", command=self._imp_tree.yview)
        self._imp_tree.configure(yscrollcommand=vsb.set)
        self._imp_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._imp_tree.tag_configure("on",      foreground="#237804")   # 深绿 在架
        self._imp_tree.tag_configure("off",     foreground="#ad4e00")   # 深橙 未上架
        self._imp_tree.tag_configure("sold",    foreground="#595959")   # 深灰 已售
        self._imp_tree.tag_configure("unknown", foreground="#003a8c")   # 深蓝 未知

        # 右键删除单条
        menu = tk.Menu(win, tearoff=0)
        menu.add_command(label="🗑 删除此条", command=self._imp_delete_selected)
        self._imp_tree.bind("<Button-2>", lambda e: (
            self._imp_tree.identify_row(e.y) and
            self._imp_tree.selection_set(self._imp_tree.identify_row(e.y)),
            menu.tk_popup(e.x_root, e.y_root),
        ))

        self._refresh_import_tree()

    def _imp_delete_selected(self):
        sel = self._imp_tree.selection()
        if not sel: return
        iid = sel[0]
        # iid = product_id
        self._batch_items = [i for i in self._batch_items if i.product_id != iid]
        self._refresh_import_tree()
        self._update_import_status_label()

    def _refresh_import_tree(self):
        """重新渲染浮窗 Treeview（不拉网络）"""
        if not hasattr(self, "_imp_tree") or not self._imp_tree.winfo_exists():
            return
        for row in self._imp_tree.get_children():
            self._imp_tree.delete(row)

        # 获取状态筛选
        active_filters = set()
        if hasattr(self, "_imp_status_filters"):
            active_filters = {name for name, var in self._imp_status_filters.items() if var.get()}

        for item in self._batch_items:
            sc = str(item.status_code)
            sn = item.status_name
            if sc == "0" or sn in ("已上架", "在售"):
                tag, st = "on",  "已上架"
            elif sc == "60" or sn == "未上架":
                tag, st = "off", "未上架"
            elif sc == "80" or sn in ("已售出", "已售"):
                tag, st = "sold","已售"
            else:
                tag, st = "unknown", sn or sc or "未知"

            # 状态筛选
            if active_filters and st not in active_filters:
                continue

            sug = f"¥{item.suggested_price}" if item.suggested_price else "—"
            self._imp_tree.insert("", tk.END, iid=item.product_id, tags=(tag,),
                                  values=(item.qc_code, item.model, item.condition,
                                          item.capacity, item.color, st,
                                          f"¥{int(item.current_price)}", sug))
        self._update_import_status_label()

    def _update_import_status_label(self):
        total    = len(self._batch_items)
        # 兼容 status_code 为 "0"/0/整数 及 status_name 文字两种情况
        on_sale  = sum(1 for i in self._batch_items
                       if str(i.status_code) == "0" or i.status_name in ("已上架", "在售"))
        unlisted = sum(1 for i in self._batch_items
                       if str(i.status_code) == "60" or i.status_name == "未上架")
        msg = f"共 {total} 件  在架 {on_sale}  未上架 {unlisted}"
        self.lbl_auto_import_status.config(text=msg, fg="#389e0d" if total else "#aaaaaa")
        if hasattr(self, "_imp_status_lbl") and self._imp_status_lbl.winfo_exists():
            self._imp_status_lbl.config(text=msg)

    def _refresh_import_status(self):
        """重新拉取所有导入商品的最新状态"""
        cookie = self._get_imp_cookie()
        if not cookie:
            self._auto_log("❌ 刷新状态：未选择操作店铺"); return
        if not self._batch_items:
            return
        codes = [i.qc_code for i in self._batch_items if i.qc_code]
        imeis = [i.imei    for i in self._batch_items if i.imei and not i.qc_code]
        all_codes = codes + imeis
        if not all_codes: return

        self.root.after(0, lambda: self._imp_status_lbl.config(
            text="刷新中...", fg="#1890ff") if hasattr(self, "_imp_status_lbl") and
            self._imp_status_lbl.winfo_exists() else None)
        try:
            svc = ImeiService(cookie)
            found, _ = svc.fetch_by_codes(all_codes)
        except Exception as e:
            self._auto_log(f"❌ 刷新状态失败：{e}"); return

        raw_map = {}
        for r in found:
            k = r.get("qcCode") or r.get("imei") or ""
            if k: raw_map[k] = r

        for item in self._batch_items:
            r = raw_map.get(item.qc_code) or raw_map.get(item.imei) or {}
            if not r: continue
            state      = r.get("state") or {}
            _sv = state.get("status") if state.get("status") is not None else None
            item.status_code = str(_sv) if _sv is not None else ""
            item.status_name = state.get("statusName") or ""
            pi = r.get("priceInfo") or {}
            price = pi.get("sellingPrice") or 0
            if price: item.current_price = price / 100

            # 更新成色（从 properties 提取）
            properties = r.get("properties", [])
            if properties:
                new_condition = build_condition(properties)
                if new_condition != "未知成色":
                    item.condition = new_condition

            # 同步建议价
            if self.engine:
                res = self.engine.query(item.model, item.condition,
                                        item.capacity, item.color)
                if res: item.suggested_price = res.suggested_price

        self.root.after(0, self._refresh_import_tree)
        self._auto_log(f"🔄 已刷新 {len(self._batch_items)} 件商品状态")

    def _do_import_from_erp_imei(self):
        """从爱管机 ERP 提取 IMEI1，批量查转转并导入商品管理（在架+未上架）"""
        cookie = self._get_imp_cookie()
        if not cookie:
            self._auto_log("❌ ERP 导入：请先在商品管理窗口选择操作店铺"); return

        token   = self.erp_config.token
        version = self.erp_config.version
        if not token:
            self._auto_log("❌ ERP 导入：请先在设置中填写并保存爱管机 Token"); return

        self._auto_log("📥 ERP 导入：正在拉取爱管机在库/已上架数据...")
        try:
            fetcher  = ErpFetcher(token, version)
            all_raw: list[dict] = []
            shelf = fetcher.fetch_put_shelf(on_log=self._auto_log)
            all_raw.extend(shelf)
            self._auto_log(f"  已上架：{len(shelf)} 条")
            stock = fetcher.fetch_stock(on_log=self._auto_log)
            all_raw.extend(stock)
            self._auto_log(f"  在库：{len(stock)} 条")
        except Exception as e:
            self._auto_log(f"❌ ERP 拉取失败：{e}"); return

        # 提取 IMEI1（erp 字段名 "imei"），15位数字，去重
        _imei_re = re.compile(r"\d{15}")
        seen_imei: set[str] = set()
        imeis: list[str]    = []
        for it in all_raw:
            imei = str(it.get("imei") or "").strip()
            if imei and _imei_re.fullmatch(imei) and imei not in seen_imei:
                seen_imei.add(imei)
                imeis.append(imei)

        if not imeis:
            self._auto_log("⚠️ ERP 数据中未找到有效 IMEI1（15位数字），请确认爱管机是否已录入 IMEI1 字段")
            return

        self._auto_log(f"🔍 共提取 {len(imeis)} 个 IMEI，开始查询转转（在架+未上架）...")

        def _progress(done, total):
            self.root.after(0, lambda d=done, t=total: self._imp_status_lbl.config(
                text=f"查询中 {d}/{t}...", fg="#1890ff"
            ) if hasattr(self, "_imp_status_lbl") and self._imp_status_lbl.winfo_exists() else None)

        try:
            svc = ImeiService(cookie)
            found, not_found = svc.fetch_by_codes(
                imeis, on_progress=_progress, status_list=["0", "60", "80"]
            )
        except Exception as e:
            self._auto_log(f"❌ 转转查询失败：{e}"); return

        existing_ids = {i.product_id for i in self._batch_items}
        added = 0
        for r in found:
            item = self._parse_raw_to_batch_item(r)
            if self.engine:
                res = self.engine.query(item.model, item.condition,
                                        item.capacity, item.color)
                if res:
                    item.suggested_price = res.suggested_price
                    item.floor_price     = res.floor_price
            if item.product_id not in existing_ids:
                self._batch_items.append(item)
                existing_ids.add(item.product_id)
                added += 1
                self._auto_log(
                    f"  ✅ [{item.imei or item.qc_code}] {item.model} "
                    f"{item.condition} {item.capacity} {item.color}"
                    f" — {item.status_name or item.status_code}"
                    f"  ¥{int(item.current_price)}"
                    + (f"  建议¥{item.suggested_price}" if item.suggested_price else "")
                )

        if not_found:
            self._auto_log(
                f"  ⚠️ 转转未找到 {len(not_found)} 个"
                f"（可能未在转转上架，或已售出）"
            )

        self._auto_log(f"✅ ERP 导入完成：新增 {added} 件（共 {len(self._batch_items)} 件）")
        self.root.after(0, self._refresh_import_tree)

    def _imp_refresh_accounts(self):
        names = [a.name for a in self.account_mgr.accounts]
        self._imp_account_combo["values"] = names
        if names and not self._imp_account_combo.get():
            self._imp_account_combo.set(names[0])

    def _toggle_imp_auto_refresh(self):
        self._imp_refresh_running = not self._imp_refresh_running
        if self._imp_refresh_running:
            self._imp_auto_refresh_btn.config(text="⏹ 停止定时刷新", bg="#ff4d4f")
            self._auto_log("⏰ 商品管理：定时刷新已启动")
            threading.Thread(target=self._imp_auto_refresh_loop, daemon=True).start()
        else:
            self._imp_auto_refresh_btn.config(text="⏰ 开启定时刷新", bg="#722ed1")
            self._imp_next_refresh_lbl.config(text="")
            self._auto_log("⏹ 商品管理：定时刷新已停止")

    def _imp_auto_refresh_loop(self):
        import time as _time
        while self._imp_refresh_running:
            try:
                interval_min = float(self._imp_refresh_interval.get())
            except Exception:
                interval_min = 5
            interval_s = max(60, interval_min * 60)

            # 倒计时显示
            deadline = _time.time() + interval_s
            while self._imp_refresh_running:
                remaining = int(deadline - _time.time())
                if remaining <= 0:
                    break
                mins, secs = divmod(remaining, 60)
                label = f"下次刷新：{mins:02d}:{secs:02d}"
                self.root.after(0, lambda t=label: (
                    hasattr(self, "_imp_next_refresh_lbl") and
                    self._imp_next_refresh_lbl.winfo_exists() and
                    self._imp_next_refresh_lbl.config(text=t)
                ))
                _time.sleep(1)

            if not self._imp_refresh_running:
                break

            # 执行刷新，对比状态变化
            self._imp_do_auto_refresh()

        self.root.after(0, lambda: (
            hasattr(self, "_imp_next_refresh_lbl") and
            self._imp_next_refresh_lbl.winfo_exists() and
            self._imp_next_refresh_lbl.config(text="")
        ))

    def _imp_do_auto_refresh(self):
        """定时刷新：拉取最新状态，对比变化，有变化时推送企业微信"""
        if not self._batch_items:
            return
        cookie = self._get_imp_cookie()
        if not cookie:
            return

        # 记录刷新前状态快照
        before = {i.product_id: (i.status_code, i.status_name, i.current_price)
                  for i in self._batch_items}

        try:
            svc = ImeiService(cookie)
            all_codes = [i.qc_code for i in self._batch_items if i.qc_code] + \
                        [i.imei    for i in self._batch_items if i.imei and not i.qc_code]
            if not all_codes:
                return
            found, _ = svc.fetch_by_codes(all_codes)
        except Exception as e:
            self._auto_log(f"⚠️ 定时刷新失败：{e}")
            return

        raw_map: dict[str, dict] = {}
        for r in found:
            k = r.get("qcCode") or r.get("imei") or ""
            if k: raw_map[k] = r

        changes: list[str] = []
        newly_unlisted: list[str] = []   # 质检中 → 未上架
        newly_on_sale:  list[str] = []   # 未上架 → 已上架

        for item in self._batch_items:
            r = raw_map.get(item.qc_code) or raw_map.get(item.imei) or {}
            if not r:
                continue
            state = r.get("state") or {}
            _sv = state.get("status")
            new_code = str(_sv) if _sv is not None else item.status_code
            new_name = state.get("statusName") or item.status_name
            pi = r.get("priceInfo") or {}
            new_price = (pi.get("sellingPrice") or 0) / 100 or item.current_price

            # 更新成色（从 properties 提取）
            properties = r.get("properties", [])
            if properties:
                new_condition = build_condition(properties)
                if new_condition != "未知成色":
                    item.condition = new_condition

            old_code, old_name, old_price = before.get(item.product_id, ("", "", 0))

            changed = False
            desc_parts = []

            if new_code != old_code or new_name != old_name:
                desc_parts.append(f"状态 {old_name or old_code} → {new_name or new_code}")
                changed = True
                # 分类变化类型
                if new_code == "60" and old_code not in ("60",):
                    newly_unlisted.append(
                        f"[{item.qc_code or item.imei}] {item.model} {item.condition} "
                        f"{item.capacity} {item.color}"
                    )
                elif new_code == "0" and old_code == "60":
                    newly_on_sale.append(
                        f"[{item.qc_code or item.imei}] {item.model} {item.condition} "
                        f"{item.capacity} {item.color}  ¥{int(new_price)}"
                    )

            if abs(new_price - old_price) > 0.5:
                desc_parts.append(f"价格 ¥{int(old_price)} → ¥{int(new_price)}")
                changed = True

            if changed:
                item.status_code  = new_code
                item.status_name  = new_name
                item.current_price = new_price
                changes.append(
                    f"[{item.qc_code or item.imei}] {item.model} {item.condition} "
                    f"{item.capacity}  {'、'.join(desc_parts)}"
                )

        self.root.after(0, self._refresh_import_tree)

        if changes:
            now_str = datetime.now().strftime("%H:%M:%S")
            for c in changes:
                self._auto_log(f"🔔 [{now_str}] 状态变化：{c}")

            # 推送企业微信
            lines = [f"🔔 商品状态变化  {datetime.now().strftime('%m/%d %H:%M')}"]
            if newly_unlisted:
                lines.append(f"📦 质检完成→未上架（{len(newly_unlisted)} 件）")
                for s in newly_unlisted[:8]:
                    lines.append(f"  · {s}")
            if newly_on_sale:
                lines.append(f"✅ 已上架（{len(newly_on_sale)} 件）")
                for s in newly_on_sale[:8]:
                    lines.append(f"  · {s}")
            other = [c for c in changes
                     if not any(c.startswith(f"[{i.qc_code}") for i in self._batch_items
                                if f"[{i.qc_code}]" in (newly_unlisted + newly_on_sale))]
            if other:
                lines.append(f"📝 其他变化（{len(other)} 件）")
                for s in other[:5]:
                    lines.append(f"  · {s}")
            self._wxwork_notify("\n".join(lines))
        else:
            self._auto_log(f"🔄 定时刷新完成，无状态变化（共 {len(self._batch_items)} 件）")

    def _get_imp_cookie(self) -> Optional[str]:
        """取商品管理窗口的 cookie，回退到自动化 tab 的选择"""
        name = ""
        if hasattr(self, "_imp_account_combo") and self._imp_account_combo.winfo_exists():
            name = self._imp_account_combo.get()
        if not name and hasattr(self, "combo_auto_account"):
            name = self.combo_auto_account.get()
        for a in self.account_mgr.accounts:
            if a.name == name:
                return a.cookie
        return None

    def _do_import_by_codes(self):
        """按质检码/IMEI 查询并追加到 _batch_items（浮窗版）"""
        cookie = self._get_imp_cookie()
        if not cookie:
            self._auto_log("❌ 请先在商品管理窗口选择操作店铺"); return

        raw_text = self._imp_code_box.get("1.0", tk.END).strip()
        if not raw_text:
            self._auto_log("❌ 请先输入质检码或 IMEI"); return

        import re
        codes = [c for c in re.split(r"[\s,，;；]+", raw_text) if c]
        self._auto_log(f"🔍 查询 {len(codes)} 个质检码/IMEI...")

        try:
            svc = ImeiService(cookie)
            found, not_found = svc.fetch_by_codes(codes)
        except Exception as e:
            self._auto_log(f"❌ 查询失败：{e}"); return

        existing_ids = {i.product_id for i in self._batch_items}
        added = 0
        for r in found:
            item = self._parse_raw_to_batch_item(r)
            if self.engine:
                res = self.engine.query(item.model, item.condition,
                                        item.capacity, item.color)
                if res: item.suggested_price = res.suggested_price
            if item.product_id not in existing_ids:
                self._batch_items.append(item)
                existing_ids.add(item.product_id)
                added += 1
                sug_str = f"  建议¥{item.suggested_price}" if item.suggested_price else ""
                self._auto_log(
                    f"  ✅ [{item.qc_code or item.imei}]  {item.model} {item.condition} "
                    f"{item.capacity} {item.color}  "
                    f"状态:{item.status_name}(code={item.status_code!r})  "
                    f"当前¥{int(item.current_price)}{sug_str}"
                )

        if not_found:
            self._auto_log(f"  ⚠️ 未找到：{' '.join(not_found[:10])}")

        self._auto_log(f"✅ 新增 {added} 件（共 {len(self._batch_items)} 件）")
        self.root.after(0, self._refresh_import_tree)

    def _auto_import_by_codes(self):
        """兼容旧调用 → 转发到新实现"""
        self._do_import_by_codes()

    def _auto_clear_items(self):
        """清空商品列表"""
        if not self._batch_items:
            return
        if not messagebox.askyesno("确认", "确定清空已导入的全部商品？"):
            return
        self._batch_items.clear()
        self._auto_log("🗑 已清空导入列表")
        self.root.after(0, self._refresh_import_tree)
        self._update_import_status_label()

    def _open_auto_log_window(self):
        """打开/激活自动化日志浮窗"""
        if hasattr(self, "_log_win") and self._log_win and self._log_win.winfo_exists():
            self._log_win.lift()
            self._log_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("📋 自动化日志")
        win.geometry("900x500")
        win.minsize(600, 300)
        self._log_win = win

        # 顶部工具栏
        bar = tk.Frame(win, bg="#2b2b2b"); bar.pack(fill=tk.X)
        tk.Button(bar, text="🗑 清空", font=self.FONT_DEFAULT,
                  bg="#2b2b2b", fg="#aaa", relief="flat",
                  command=self._auto_log_clear).pack(side=tk.LEFT, padx=8, pady=4)
        tk.Button(bar, text="📌 置顶", font=self.FONT_DEFAULT,
                  bg="#2b2b2b", fg="#aaa", relief="flat",
                  command=lambda: win.attributes("-topmost",
                      not win.attributes("-topmost"))
                  ).pack(side=tk.LEFT, padx=4, pady=4)
        self._log_topmost_lbl = tk.Label(bar, text="", font=("PingFang SC",11),
                                         bg="#2b2b2b", fg="#fa8c16")
        self._log_topmost_lbl.pack(side=tk.LEFT, padx=4)

        # 日志文本框
        frm = tk.Frame(win); frm.pack(fill=tk.BOTH, expand=True)
        sb  = ttk.Scrollbar(frm, orient="vertical")
        self._log_text = tk.Text(
            frm, bg="#1e1e1e", fg="#a9b7c6",
            font=self.FONT_MONO, padx=8, pady=6,
            yscrollcommand=sb.set, state="normal",
        )
        sb.config(command=self._log_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(fill=tk.BOTH, expand=True)

        # 把 auto_log 缓冲里积累的内容搬过来
        old_content = self.auto_log.get("1.0", tk.END)
        if old_content.strip():
            self._log_text.insert(tk.END, old_content)
            self._log_text.see(tk.END)

        def _on_close():
            self._log_win = None
            win.destroy()  # FIX: 补上 destroy，窗口才能真正关闭
        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _auto_log_clear(self):
        self.auto_log.delete("1.0", tk.END)
        if hasattr(self, "_log_text") and self._log_text.winfo_exists():
            self._log_text.config(state="normal")
            self._log_text.delete("1.0", tk.END)

    def _auto_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"

        def _update():
            # 始终写入缓冲 Text（不可见，用于持久化内容）
            self.auto_log.insert(tk.END, line)

            # 如果浮窗已打开，同步写入
            if hasattr(self, "_log_text") and self._log_text.winfo_exists():
                self._log_text.insert(tk.END, line)
                self._log_text.see(tk.END)

        self.root.after(0, _update)

    # ── 调度器总开关 ──────────────────────────────────────────────────────────
    def _run_with_log(self, fn):
        """弹出日志窗口，然后在后台线程执行 fn"""
        self._open_auto_log_window()
        threading.Thread(target=fn, daemon=True).start()

    def _open_data_lab(self):
        if not self._data_lab:
            self._data_lab = DataLabWindow(self)
        self._data_lab.open()

    def _toggle_auto_scheduler(self):
        if self._auto_running:
            self._auto_running = False
            self.btn_auto_start.config(text="▶ 启动自动化调度器", bg="#52c41a")
            self._auto_log("⏹ 调度器已停止")
        else:
            self._auto_running = True
            self.btn_auto_start.config(text="⏹ 停止调度器", bg="#ff4d4f")
            self._auto_log("▶ 调度器已启动")
            self.root.after(200, self._open_auto_log_window)   # 启动时自动弹出日志窗口
            self._auto_thread = threading.Thread(target=self._auto_scheduler_loop, daemon=True)
            self._auto_thread.start()

    def _auto_scheduler_loop(self):
        """后台调度循环：每30秒检查一次是否触发各模块"""
        import time as _time
        last_reprice_day   = ""
        last_stale_ts      = _time.time()   # 初始化为当前时间，避免启动即触发
        last_list_new_ts   = _time.time()
        last_report_times: set[str] = set()

        while self._auto_running:
            now = datetime.now()
            day_str = now.strftime("%Y-%m-%d")
            hm_str  = now.strftime("%H:%M")

            # 模块1：定时改价
            if self.auto1_enabled.get():
                target = self.auto1_time.get().strip()
                if hm_str == target and last_reprice_day != day_str:
                    last_reprice_day = day_str
                    self._auto_log(f"⏰ 定时触发：自动改价（{target}）")
                    self._auto_run_reprice()

            # 模块2：滞销检查
            if self.auto2_enabled.get():
                try:    interval_s = float(self.auto2_interval_h.get()) * 3600
                except: interval_s = 21600
                if _time.time() - last_stale_ts >= interval_s:
                    last_stale_ts = _time.time()
                    self._auto_log("⏰ 定时触发：滞销检查")
                    self._auto_run_stale()

            # 模块3：未上架扫描
            if self.auto3_enabled.get():
                try:    interval_s = float(self.auto3_interval_m.get()) * 60
                except: interval_s = 1800
                if _time.time() - last_list_new_ts >= interval_s:
                    last_list_new_ts = _time.time()
                    self._auto_log("⏰ 定时触发：未上架扫描")
                    self._auto_run_list_new()

            # 模块4：飞书销售播报
            if self.auto4_enabled.get():
                for t in [x.strip() for x in self.auto4_times.get().split(",") if x.strip()]:
                    key = f"{day_str}_{t}"
                    if hm_str == t and key not in last_report_times:
                        last_report_times.add(key)
                        # 只保留今天的 key，防止无限增长
                        last_report_times = {k for k in last_report_times if k.startswith(day_str)}
                        self._auto_log(f"⏰ 定时触发：飞书播报（{t}）")
                        self._auto_run_feishu_report()

            # 智能调价：整合模块1/2/3
            if self.auto_smart_enabled.get():
                target = self.auto_smart_time.get().strip()
                if hm_str == target and f"smart_{day_str}" not in last_report_times:
                    last_report_times.add(f"smart_{day_str}")
                    self._auto_log(f"⏰ 定时触发：智能调价（{target}）")
                    self._auto_run_smart_reprice()

            _time.sleep(30)

    # ════════════════════════════════════════════════════════════════════════
    # 模块1 实现：定时自动改价
    # ════════════════════════════════════════════════════════════════════════
    def _auto_run_reprice(self):
        import time as _time
        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ 模块1：未选择操作店铺"); return
        if self.engine is None:
            self._auto_log("❌ 模块1：无历史数据，请先同步"); return

        items = [i for i in self._batch_items
                 if str(i.status_code) == "0" or i.status_name in ("已上架", "在售")]

        self._auto_log(f"🔄 模块1：对已导入的 {len(items)} 件在架商品匹配建议价并改价...")
        svc = ImeiService(cookie)
        success = skip = fail = 0
        success_items: list[str] = []
        fail_items:    list[str] = []

        for item in items:
            result = self.engine.query(item.model, item.condition,
                                       item.capacity, item.color)
            if result is None:
                if self.auto1_no_data_skip.get():
                    self._auto_log(f"  ⏭ [{item.qc_code}] {item.model} {item.condition} {item.capacity} — 无历史数据跳过")
                    skip += 1; continue
            else:
                item.suggested_price = result.suggested_price

            if item.suggested_price == 0:
                skip += 1; continue

            old_price = int(item.current_price)
            diff = item.suggested_price - old_price
            if self.auto1_only_lower.get() and diff >= 0:
                self._auto_log(f"  ⏭ [{item.qc_code}] {item.model} {item.condition} {item.capacity} — 建议价¥{item.suggested_price} 不低于当前¥{old_price}，跳过")
                skip += 1; continue

            pd_obj = ProductDetail(
                product_id=item.product_id, sku_id="", group_key="",
                title=item.title, model=item.model, condition=item.condition,
                capacity=item.capacity, color=item.color,
                current_price=item.current_price, settle_price=0,
                status=item.status_code,
            )
            try:
                svc.update_price(pd_obj, float(item.suggested_price))
                item.current_price = float(item.suggested_price)
                real_settle = svc.fetch_settle_price(item.product_id)
                if real_settle is not None:
                    new_settle = int(real_settle)
                    settle_note = f"💰到手¥{new_settle}（转转实时）"
                else:
                    if item.settle_price and pd_obj.current_price:
                        fee_rate = 1 - (item.settle_price / pd_obj.current_price)
                    else:
                        fee_rate = PLATFORM_FEE_RATE
                    new_settle = round(item.suggested_price * (1 - fee_rate) - STATION_SERVICE_FEE)
                    settle_note = f"💰到手约¥{new_settle}（估算）"
                item.settle_price = float(new_settle)
                arrow = "↓" if diff < 0 else "↑"
                success += 1
                line = (f"[{item.qc_code}] {item.model} {item.condition} {item.capacity} "
                        f"¥{old_price}{arrow}¥{item.suggested_price}（{diff:+d}）  {settle_note}")
                self._auto_log(f"  ✅ {line}")
                success_items.append(line)
            except Exception as e:
                fail += 1
                line = f"[{item.qc_code}] {item.model} {item.condition} — {e}"
                self._auto_log(f"  ❌ {line}")
                fail_items.append(line)
            _time.sleep(1)

        summary = f"模块1 完成：✅成功{success} ⏭跳过{skip} ❌失败{fail}"
        self._auto_log(summary)
        self.root.after(0, self._render_batch_tree)

        # ── 企业微信汇报 ──────────────────────────────────────────────────
        if success or fail:
            now_str = datetime.now().strftime("%m/%d %H:%M")
            lines = [f"🔄 自动调价完成  {now_str}",
                     f"✅ 成功 {success} 件  ⏭ 跳过 {skip} 件  ❌ 失败 {fail} 件",
                     "━" * 24]
            for s in success_items[:10]:
                lines.append(f"  ✅ {s}")
            if len(success_items) > 10:
                lines.append(f"  ……共 {len(success_items)} 件")
            for f_item in fail_items[:5]:
                lines.append(f"  ❌ {f_item}")
            self._wxwork_notify("\n".join(lines))

    # ════════════════════════════════════════════════════════════════════════
    # 模块2 实现：滞销预警 + 自动降阶
    # ════════════════════════════════════════════════════════════════════════
    def _preview_stale_reprice(self):
        """预览滞销降价方案"""
        import time as _time
        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ 模块2预览：未选择操作店铺"); return
        if self.engine is None:
            self._auto_log("❌ 模块2预览：无历史数据，请先同步"); return

        try:
            days1 = int(self.auto2_days1.get())
            days2 = int(self.auto2_days2.get())
        except ValueError:
            self._auto_log("❌ 模块2预览：参数格式有误"); return

        items = [i for i in self._batch_items
                 if str(i.status_code) == "0" or i.status_name in ("已上架", "在售")]
        if not items:
            self._auto_log("⏭ 模块2预览：当前导入列表中无在架商品"); return

        self._auto_log(f"🔍 模块2预览：扫描 {len(items)} 件在架商品...")
        svc = ImeiService(cookie)
        now = datetime.now()

        # 拉取上架时间
        qc_codes = [i.qc_code for i in items if i.qc_code]
        raw_map: dict[str, dict] = {}
        if qc_codes:
            try:
                found, _ = svc.fetch_by_codes(qc_codes)
                raw_map = {r.get("qcCode", ""): r for r in found}
            except Exception as e:
                self._auto_log(f"⚠️ 模块2预览：拉取库龄数据失败：{e}"); return

        # 计算降价方案
        candidates = []
        stage1_strategy = self.auto2_stage1_strategy.get() if hasattr(self, "auto2_stage1_strategy") else "conservative"

        for item in items:
            raw = raw_map.get(item.qc_code, {})
            put_on = (raw.get("lifecycleTimes") or {}).get("putOnMartTime")
            if not put_on:
                continue
            try:
                age_days = (now - datetime.strptime(put_on, "%Y-%m-%d %H:%M:%S")).days
            except:
                continue

            if age_days < days1:
                continue

            # 计算降价
            result = self.engine.query(item.model, item.condition, item.capacity, item.color)
            conservative = result.suggested_price if result else 0

            # 第一阶段降价
            if stage1_strategy == "conservative":
                stage1_price = conservative if conservative > 0 else 0
            elif stage1_strategy == "percent":
                try:
                    pct = float(self.auto2_stage1_pct.get()) / 100
                    stage1_price = _round_to_8(int(item.current_price * (1 - pct)))
                except:
                    stage1_price = conservative if conservative > 0 else 0
            elif stage1_strategy == "fixed":
                try:
                    amt = int(self.auto2_stage1_amt.get())
                    stage1_price = _round_to_8(int(item.current_price) - amt)
                except:
                    stage1_price = conservative if conservative > 0 else 0
            else:
                stage1_price = conservative if conservative > 0 else 0

            if stage1_price <= 0:
                continue

            # 第二阶段
            if age_days >= days2:
                try:
                    extra_pct = float(self.auto2_extra_pct.get()) / 100
                    new_price = _round_to_8(int(stage1_price * (1 - extra_pct)))
                except:
                    new_price = stage1_price
                tier = 2
            else:
                new_price = stage1_price
                tier = 1

            if new_price >= int(item.current_price):
                continue

            candidates.append({
                "item": item,
                "age_days": age_days,
                "tier": tier,
                "current_price": int(item.current_price),
                "new_price": new_price,
                "diff": new_price - int(item.current_price),
            })

        if not candidates:
            self.root.after(0, lambda: messagebox.showinfo("预览", "没有需要降价的商品"))
            self._auto_log("✅ 模块2预览：没有需要降价的商品")
            return

        # 显示预览窗口
        self.root.after(0, lambda: self._show_stale_preview_window(candidates))

    def _show_stale_preview_window(self, candidates: list[dict]):
        """显示滞销降价预览窗口"""
        win = tk.Toplevel(self.root)
        win.title("滞销降价预览")
        win.geometry("1200x600")

        # 顶部说明
        top_frame = ttk.Frame(win)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)
        ttk.Label(top_frame, text=f"共 {len(candidates)} 件商品需要降价，请勾选要执行的商品：",
                  font=("Arial", 10, "bold")).pack(side=tk.LEFT)

        # Treeview
        tree_frame = ttk.Frame(win)
        tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

        cols = ("选择", "质检码", "型号", "成色", "容量", "颜色", "库龄", "阶段", "当前价", "建议价", "降价幅度")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=20)

        # 列宽和标题
        tree.column("选择", width=50, anchor="center")
        tree.column("质检码", width=120, anchor="center")
        tree.column("型号", width=150, anchor="w")
        tree.column("成色", width=80, anchor="center")
        tree.column("容量", width=80, anchor="center")
        tree.column("颜色", width=80, anchor="center")
        tree.column("库龄", width=60, anchor="center")
        tree.column("阶段", width=60, anchor="center")
        tree.column("当前价", width=80, anchor="e")
        tree.column("建议价", width=80, anchor="e")
        tree.column("降价幅度", width=80, anchor="e")

        for col in cols:
            tree.heading(col, text=col)

        # 滚动条
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 插入数据，默认全选
        checked_items = {}
        for cand in candidates:
            item = cand["item"]
            iid = tree.insert("", tk.END, values=(
                "☑",
                item.qc_code or "",
                item.model or "",
                item.condition or "",
                item.capacity or "",
                item.color or "",
                f"{cand['age_days']}天",
                f"阶段{cand['tier']}",
                f"¥{cand['current_price']}",
                f"¥{cand['new_price']}",
                f"{cand['diff']}"
            ))
            checked_items[iid] = {"checked": True, "candidate": cand}

        # 点击切换选择状态
        def toggle_check(event):
            region = tree.identify("region", event.x, event.y)
            if region != "cell":
                return
            col = tree.identify_column(event.x)
            if col != "#1":  # 只在第一列（选择列）响应
                return
            iid = tree.identify_row(event.y)
            if not iid:
                return

            checked_items[iid]["checked"] = not checked_items[iid]["checked"]
            current_vals = list(tree.item(iid, "values"))
            current_vals[0] = "☑" if checked_items[iid]["checked"] else "☐"
            tree.item(iid, values=current_vals)

        tree.bind("<Button-1>", toggle_check)

        # 底部按钮
        btn_frame = ttk.Frame(win)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)

        def select_all():
            for iid in checked_items:
                checked_items[iid]["checked"] = True
                current_vals = list(tree.item(iid, "values"))
                current_vals[0] = "☑"
                tree.item(iid, values=current_vals)

        def deselect_all():
            for iid in checked_items:
                checked_items[iid]["checked"] = False
                current_vals = list(tree.item(iid, "values"))
                current_vals[0] = "☐"
                tree.item(iid, values=current_vals)

        def confirm_reprice():
            selected = [info["candidate"] for info in checked_items.values() if info["checked"]]
            if not selected:
                messagebox.showwarning("提示", "请至少选择一件商品")
                return

            if not messagebox.askyesno("确认", f"确定要对选中的 {len(selected)} 件商品执行降价吗？"):
                return

            win.destroy()
            self._execute_stale_reprice(selected)

        ttk.Button(btn_frame, text="全选", command=select_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="全不选", command=deselect_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="确认调价", command=confirm_reprice).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=win.destroy).pack(side=tk.RIGHT, padx=5)

    def _execute_stale_reprice(self, selected_candidates: list[dict]):
        """执行选中商品的降价"""
        import time as _time
        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ 执行降价：未选择操作店铺")
            return

        self._auto_log(f"🔄 开始执行 {len(selected_candidates)} 件商品的降价...")
        svc = ImeiService(cookie)
        success = fail = 0

        for cand in selected_candidates:
            item = cand["item"]
            new_price = cand["new_price"]
            tier = cand["tier"]
            age_days = cand["age_days"]

            pd_obj = ProductDetail(
                product_id=item.product_id, sku_id="", group_key="",
                title=item.title, model=item.model, condition=item.condition,
                capacity=item.capacity, color=item.color,
                current_price=item.current_price, settle_price=0,
                status=item.status_code,
            )

            try:
                svc.update_price(pd_obj, float(new_price))
                old_price = int(item.current_price)
                item.current_price = float(new_price)
                self._auto_log(
                    f"  ✅ [阶段{tier}] [{item.qc_code}] {item.model} {item.condition} {item.capacity} "
                    f"库龄{age_days}天 — ¥{old_price} ↓ ¥{new_price}（降{old_price - new_price}元）"
                )
                success += 1
            except Exception as e:
                self._auto_log(f"  ❌ [{item.qc_code}] {item.model} — 改价失败：{e}")
                fail += 1

            _time.sleep(1)

        self._auto_log(f"✅ 降价完成：成功{success}件，失败{fail}件")
        self.root.after(0, self._render_batch_tree)

    def _auto_run_stale(self):
        import time as _time
        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ 模块2：未选择操作店铺"); return
        if self.engine is None:
            self._auto_log("❌ 模块2：无历史数据，请先同步"); return

        try:
            days1     = int(self.auto2_days1.get())
            days2     = int(self.auto2_days2.get())
            use_fixed = self.auto2_use_fixed.get()
            extra_pct = float(self.auto2_extra_pct.get()) / 100
            extra_amt = int(self.auto2_extra_amt.get())
        except ValueError:
            self._auto_log("❌ 模块2：参数格式有误"); return

        items = [i for i in self._batch_items
                 if str(i.status_code) == "0" or i.status_name in ("已上架", "在售")]
        if not items:
            self._auto_log("⏭ 模块2：当前导入列表中无在架商品，跳过"); return

        self._auto_log(f"🔄 模块2：检查已导入的 {len(items)} 件在架商品库龄...")
        alert_only = self.auto2_alert_only.get()
        svc = ImeiService(cookie)
        warn = act1 = act2 = skip = 0
        now = datetime.now()
        success_items: list[str] = []
        warn_items:    list[str] = []

        # 需要从原始 API 数据获取上架时间，_batch_items 里没有，用 product_id 在 _raw_cache 里查
        # 简化方案：直接用 fetch_by_codes 重新拉取上架时间
        qc_codes = [i.qc_code for i in items if i.qc_code]
        raw_map: dict[str, dict] = {}
        if qc_codes:
            try:
                found, _ = svc.fetch_by_codes(qc_codes)
                raw_map = {r.get("qcCode", ""): r for r in found}
            except Exception as e:
                self._auto_log(f"⚠️ 模块2：补充拉取库龄数据失败：{e}")

        for item in items:
            raw = raw_map.get(item.qc_code, {})
            put_on = (raw.get("lifecycleTimes") or {}).get("putOnMartTime")
            if not put_on:
                self._auto_log(f"  ⏭ {item.qc_code or item.title[:16]} 无上架时间，跳过")
                skip += 1; continue
            try:
                age_days = (now - datetime.strptime(put_on, "%Y-%m-%d %H:%M:%S")).days
            except Exception:
                skip += 1; continue

            if age_days < days1:
                continue

            # 获取历史数据
            result = self.engine.query(item.model, item.condition,
                                       item.capacity, item.color)
            conservative = result.suggested_price if result else 0

            # 计算第一阶段降价
            stage1_strategy = self.auto2_stage1_strategy.get() if hasattr(self, "auto2_stage1_strategy") else "conservative"

            if age_days >= days1:
                if stage1_strategy == "conservative":
                    # 降至保守动销价
                    stage1_price = conservative if conservative > 0 else 0
                elif stage1_strategy == "percent":
                    # 按百分比降价
                    try:
                        pct = float(self.auto2_stage1_pct.get()) / 100
                        stage1_price = _round_to_8(int(item.current_price * (1 - pct)))
                    except:
                        stage1_price = conservative if conservative > 0 else 0
                elif stage1_strategy == "fixed":
                    # 固定金额降价
                    try:
                        amt = int(self.auto2_stage1_amt.get())
                        stage1_price = _round_to_8(int(item.current_price) - amt)
                    except:
                        stage1_price = conservative if conservative > 0 else 0
                else:
                    stage1_price = conservative if conservative > 0 else 0

                if stage1_price <= 0:
                    self._auto_log(f"  ⚠️ {item.qc_code or item.title[:16]} 库龄{age_days}天，无法计算降价")
                    warn += 1
                    continue

            # 第二阶段：在第一阶段基础上再降
            if age_days >= days2 and stage1_price > 0:
                if use_fixed:
                    new_price = _round_to_8(stage1_price - extra_amt)
                else:
                    new_price = _round_to_8(int(stage1_price * (1 - extra_pct)))
                tier = 2
            elif age_days >= days1 and stage1_price > 0:
                new_price = stage1_price
                tier = 1
            else:
                self._auto_log(
                    f"  ⚠️ {item.qc_code or item.title[:16]} 库龄{age_days}天，无历史数据"
                )
                warn += 1; continue

            if new_price >= int(item.current_price):
                skip += 1; continue

            if alert_only:
                line = (f"[{item.qc_code}] {item.model} {item.condition} {item.capacity} "
                        f"库龄{age_days}天 当前¥{int(item.current_price)} 建议¥{new_price}")
                self._auto_log(
                    f"  ⚠️ [阶段{tier}] [{item.qc_code}] {item.model} {item.condition} {item.capacity} "
                    f"库龄{age_days}天 — 当前¥{int(item.current_price)} 建议降至¥{new_price}"
                )
                warn_items.append(line)
                warn += 1
            else:
                pd_obj = ProductDetail(
                    product_id=item.product_id, sku_id="", group_key="",
                    title=item.title, model=item.model, condition=item.condition,
                    capacity=item.capacity, color=item.color,
                    current_price=item.current_price, settle_price=0,
                    status=item.status_code,
                )
                try:
                    svc.update_price(pd_obj, float(new_price))
                    old_price = int(item.current_price)
                    item.current_price = float(new_price)
                    line = (f"[{item.qc_code}] {item.model} {item.condition} {item.capacity} "
                            f"库龄{age_days}天 ¥{old_price}↓¥{new_price}（降{old_price - new_price}元）")
                    self._auto_log(
                        f"  ✅ [阶段{tier}] [{item.qc_code}] {item.model} {item.condition} {item.capacity} "
                        f"库龄{age_days}天 — ¥{old_price} ↓ ¥{new_price}（降{old_price - new_price}元）"
                    )
                    success_items.append(f"[阶段{tier}] {line}")
                    if tier == 1: act1 += 1
                    else:         act2 += 1
                except Exception as e:
                    self._auto_log(f"  ❌ [{item.qc_code}] {item.model} — 改价失败：{e}")
                _time.sleep(1)

        if alert_only:
            self._auto_log(f"模块2 完成：⚠️预警{warn} ⏭跳过{skip}")
        else:
            self._auto_log(
                f"模块2 完成：阶段1降价{act1}件 阶段2降价{act2}件 ⚠️预警{warn} ⏭跳过{skip}"
            )
        self.root.after(0, self._render_batch_tree)

        # ── 企业微信汇报 ──────────────────────────────────────────────────
        now_str = datetime.now().strftime("%m/%d %H:%M")
        if alert_only and warn_items:
            lines = [f"⏳ 滞销预警  {now_str}",
                     f"⚠️ 预警 {warn} 件  ⏭ 跳过 {skip} 件",
                     "━" * 24]
            for w in warn_items[:10]:
                lines.append(f"  ⚠️ {w}")
            if len(warn_items) > 10:
                lines.append(f"  ……共 {len(warn_items)} 件")
            self._wxwork_notify("\n".join(lines))
        elif not alert_only and success_items:
            lines = [f"⏳ 滞销降价完成  {now_str}",
                     f"✅ 阶段1降价{act1}件  阶段2降价{act2}件  ⏭跳过{skip}件",
                     "━" * 24]
            for s in success_items[:10]:
                lines.append(f"  ✅ {s}")
            if len(success_items) > 10:
                lines.append(f"  ……共 {len(success_items)} 件")
            self._wxwork_notify("\n".join(lines))

    # ════════════════════════════════════════════════════════════════════════
    # 模块3 实现：未上架自动定价上架

    # ════════════════════════════════════════════════════════════════════════
    # 智能调价实现：整合模块1/2/3
    # ════════════════════════════════════════════════════════════════════════
    def _auto_run_smart_reprice(self):
        """智能调价：扫描所有需要调价的商品并执行调价"""
        import time as _time
        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ 智能调价：未选择操作店铺"); return
        if self.engine is None:
            self._auto_log("❌ 智能调价：无历史数据，请先同步"); return
        if not self._batch_items:
            self._auto_log("❌ 智能调价：无商品数据，请先导入商品"); return

        self._auto_log("🎯 智能调价：开始扫描需要调价的商品...")
        
        candidates = []
        
        # 模块1：已上架商品按建议价调价
        module1_items = [i for i in self._batch_items
                       if str(i.status_code) == "0" or i.status_name in ("已上架", "在售")]
        
        for item in module1_items:
            result = self.engine.query(item.model, item.condition,
                                     item.capacity, item.color, "未知")
            if result and result.suggested_price > 0:
                diff = result.suggested_price - int(item.current_price)
                if diff != 0:
                    candidates.append({
                        "module": "建议价调价",
                        "item": item,
                        "new_price": result.suggested_price,
                        "diff": diff,
                        "reason": "历史数据建议价",
                    })
        
        # 模块2：滞销商品降价
        try:
            days1 = int(self.auto2_days1.get()) if hasattr(self, "auto2_days1") else 7
            days2 = int(self.auto2_days2.get()) if hasattr(self, "auto2_days2") else 14
        except:
            days1, days2 = 7, 14
        
        svc = ImeiService(cookie)
        qc_codes = [i.qc_code for i in module1_items if i.qc_code]
        raw_map = {}
        if qc_codes:
            try:
                found, _ = svc.fetch_by_codes(qc_codes)
                raw_map = {r.get("qcCode", ""): r for r in found}
            except:
                pass
        
        now = datetime.now()
        for item in module1_items:
            raw = raw_map.get(item.qc_code, {})
            put_on = (raw.get("lifecycleTimes") or {}).get("putOnMartTime")
            if not put_on:
                continue
            try:
                age_days = (now - datetime.strptime(put_on, "%Y-%m-%d %H:%M:%S")).days
            except:
                continue
            
            if age_days >= days1:
                result = self.engine.query(item.model, item.condition,
                                         item.capacity, item.color, "未知")
                if result and result.suggested_price > 0:
                    if age_days >= days2:
                        new_price = int(result.suggested_price * 0.95)
                        tier = 2
                    else:
                        new_price = result.suggested_price
                        tier = 1
                    
                    if new_price < int(item.current_price):
                        # 检查是否已在候选列表中
                        exists = any(c["item"].qc_code == item.qc_code for c in candidates)
                        if not exists:
                            candidates.append({
                                "module": f"滞销降价T{tier}",
                                "item": item,
                                "new_price": new_price,
                                "diff": new_price - int(item.current_price),
                                "reason": f"库龄{age_days}天",
                            })
        
        # 模块3：未上架商品定价
        module3_items = [i for i in self._batch_items
                       if str(i.status_code) == "60" or i.status_name == "未上架"]
        for item in module3_items:
            if item.suggested_price > 0:
                candidates.append({
                    "module": "未上架定价",
                    "item": item,
                    "new_price": item.suggested_price,
                    "diff": item.suggested_price,
                    "reason": "首次定价",
                })
        
        if not candidates:
            self._auto_log("✅ 智能调价：未发现需要调价的商品")
            return
        
        self._auto_log(f"🎯 智能调价：发现 {len(candidates)} 件需要调价的商品")
        
        # 如果开启了自动确认，直接执行调价
        auto_confirm = self.auto_smart_auto_confirm.get() if hasattr(self, "auto_smart_auto_confirm") else False
        
        if not auto_confirm:
            self._auto_log("⚠️ 智能调价：需要人工确认，请前往「智能调价看板」查看并确认")
            # 将候选商品填充到看板
            self._reprice_candidates = [{
                "module": c["module"],
                "qc_code": c["item"].qc_code,
                "model": c["item"].model,
                "condition": c["item"].condition,
                "capacity": c["item"].capacity,
                "color": c["item"].color,
                "current_price": int(c["item"].current_price),
                "suggested_price": c["new_price"],
                "diff": c["diff"],
                "settle_price": 0,
                "reason": c["reason"],
                "checked": c["diff"] < 0,
                "item": c["item"],
            } for c in candidates]
            return
        
        # 自动执行调价
        self._auto_log("🚀 智能调价：自动执行调价...")
        success = fail = 0
        logs = []
        
        for cand in candidates:
            item = cand["item"]
            pd_obj = ProductDetail(
                product_id=item.product_id, sku_id="", group_key="",
                title=item.title, model=item.model, condition=item.condition,
                capacity=item.capacity, color=item.color,
                current_price=item.current_price, settle_price=0,
                status=item.status_code,
            )
            try:
                svc.update_price(pd_obj, float(cand["new_price"]))
                item.current_price = float(cand["new_price"])
                
                real_settle = svc.fetch_settle_price(item.product_id)
                settle_price = int(real_settle) if real_settle else 0
                
                log_entry = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "shop": self.combo_auto_account.get(),
                    "qc_code": item.qc_code,
                    "model": item.model,
                    "condition": item.condition,
                    "capacity": item.capacity,
                    "color": item.color,
                    "old_price": int(item.current_price) - cand["diff"],
                    "new_price": cand["new_price"],
                    "diff": cand["diff"],
                    "settle_price": settle_price,
                    "module": cand["module"],
                    "reason": cand["reason"],
                    "status": "成功",
                }
                logs.append(log_entry)
                success += 1
                self._auto_log(f"  ✅ [{item.qc_code}] {item.model} {item.condition} "
                             f"¥{log_entry['old_price']}→¥{cand['new_price']} ({cand['diff']:+d})")
                
            except Exception as e:
                log_entry = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "shop": self.combo_auto_account.get(),
                    "qc_code": item.qc_code,
                    "model": item.model,
                    "condition": item.condition,
                    "capacity": item.capacity,
                    "color": item.color,
                    "old_price": int(item.current_price),
                    "new_price": cand["new_price"],
                    "diff": cand["diff"],
                    "settle_price": 0,
                    "module": cand["module"],
                    "reason": cand["reason"],
                    "status": f"失败: {e}",
                }
                logs.append(log_entry)
                fail += 1
                self._auto_log(f"  ❌ [{item.qc_code}] {item.model} — {e}")
            
            _time.sleep(1)
        
        # 保存日志
        if logs:
            self._save_reprice_logs(logs)
        
        self._auto_log(f"✅ 智能调价完成：成功 {success} 件，失败 {fail} 件")

    # ════════════════════════════════════════════════════════════════════════
    def _auto_run_list_new(self):
        import time as _time
        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ 模块3：未选择操作店铺"); return
        if self.engine is None:
            self._auto_log("❌ 模块3：无历史数据，请先同步"); return

        items = [i for i in self._batch_items
                 if i.status_code == "60" or i.status_name == "未上架"]
        if not items:
            self._auto_log("⏭ 模块3：当前导入列表中无未上架商品，跳过（如需定价，请在商品管理窗口导入未上架机器）"); return

        self._auto_log(f"🔄 模块3：对已导入的 {len(items)} 件未上架商品匹配定价...")
        svc = ImeiService(cookie)
        success = skip = fail = 0
        success_items: list[str] = []
        fail_items:    list[str] = []

        for item in items:
            result = self.engine.query(item.model, item.condition,
                                       item.capacity, item.color)
            if result is None:
                if self.auto3_no_data_skip.get():
                    self._auto_log(f"  ⏭ [{item.qc_code}] {item.model} {item.condition} {item.capacity} — 无历史数据跳过")
                    skip += 1; continue
                else:
                    skip += 1; continue

            new_price = result.suggested_price
            pd_obj = ProductDetail(
                product_id=item.product_id, sku_id="", group_key="",
                title=item.title, model=item.model, condition=item.condition,
                capacity=item.capacity, color=item.color,
                current_price=0.0, settle_price=0, status="60",
            )
            try:
                svc.update_price(pd_obj, float(new_price))
                real_settle = svc.fetch_settle_price(item.product_id)
                if real_settle is not None:
                    new_settle = int(real_settle)
                    settle_note = f"💰到手¥{new_settle}（转转实时）"
                else:
                    new_settle = round(new_price * (1 - PLATFORM_FEE_RATE) - STATION_SERVICE_FEE)
                    settle_note = f"💰到手约¥{new_settle}（估算）"
                item.current_price = float(new_price)
                item.settle_price  = float(new_settle)
                item.status_code   = "0"
                item.status_name   = "已上架"
                success += 1
                line = (f"[{item.qc_code}] {item.model} {item.condition} {item.capacity} "
                        f"{item.color} 定价¥{new_price}  {settle_note}")
                self._auto_log(f"  ✅ {line}  未上架→已上架")
                success_items.append(line)
            except Exception as e:
                fail += 1
                line = f"[{item.qc_code}] {item.model} {item.condition} — {e}"
                self._auto_log(f"  ❌ {line}")
                fail_items.append(line)
            _time.sleep(1)

        self._auto_log(f"模块3 完成：✅上架{success} ⏭跳过{skip} ❌失败{fail}")
        self.root.after(0, self._render_batch_tree)

        # ── 企业微信汇报 ──────────────────────────────────────────────────
        if success or fail:
            now_str = datetime.now().strftime("%m/%d %H:%M")
            lines = [f"🚀 自动定价上架完成  {now_str}",
                     f"✅ 上架 {success} 件  ⏭ 跳过 {skip} 件  ❌ 失败 {fail} 件",
                     "━" * 24]
            for s in success_items[:10]:
                lines.append(f"  ✅ {s}")
            if len(success_items) > 10:
                lines.append(f"  ……共 {len(success_items)} 件")
            for f_item in fail_items[:5]:
                lines.append(f"  ❌ {f_item}")
            self._wxwork_notify("\n".join(lines))

    # ════════════════════════════════════════════════════════════════════════
    # 公共：企业微信推送（供各模块操作汇报使用）
    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    # 模块5：企业微信自建应用双向对话
    # ════════════════════════════════════════════════════════════════════════

    def _wxapp_save_config(self):
        """保存自建应用配置到本地"""
        self.wxapp_config.corp_id        = self.wx5_corpid.get().strip()
        self.wxapp_config.agent_id       = self.wx5_agentid.get().strip()
        self.wxapp_config.secret         = self.wx5_secret.get().strip()
        self.wxapp_config.token          = self.wx5_token.get().strip()
        self.wxapp_config.aes_key        = self.wx5_aeskey.get().strip()
        self.wxapp_config.ngrok_authtoken = self.wx5_ngrok_token.get().strip()
        self.wxapp_config.save()
        # 重建 client（配置变了）
        self.wxapp_client = WxAppClient(self.wxapp_config)
        self._auto_log("✅ 企业微信自建应用配置已保存")
        self.root.after(0, lambda: self.lbl_wxapp_status.config(text="✅ 配置已保存", fg="#389e0d"))

    def _wxapp_test(self):
        """测试：获取AccessToken并发一条测试消息给自己"""
        if not self.wxapp_config.is_configured:
            self._auto_log("❌ 请先填写并保存 CorpID / AgentID / Secret"); return
        try:
            token = self.wxapp_client.get_access_token()
            self._auto_log(f"✅ AccessToken 获取成功（前8位：{token[:8]}…）")
            ok = self.wxapp_client.send_text(
                "🤖 转转调价助手连接测试成功！\n\n发送「帮助」查看可用指令。",
                to_user="@all"
            )
            if ok:
                self._auto_log("✅ 测试消息已发送到企业微信应用")
                self.root.after(0, lambda: self.lbl_wxapp_status.config(
                    text="✅ 连接正常", fg="#389e0d"))
            else:
                self._auto_log("⚠️ 消息发送失败，请检查 AgentID 是否正确")
        except Exception as e:
            self._auto_log(f"❌ 连接测试失败：{e}")

    def _wxapp_toggle_server(self):
        """启动/停止本地 HTTP 回调服务器（启动流程放后台线程，避免卡 UI）"""
        if self._wxapp_running:
            threading.Thread(target=self._wxapp_stop_server, daemon=True).start()
        else:
            # 禁用按钮防止重复点击
            self.btn_wxapp_server.config(state="disabled")
            threading.Thread(
                target=self._wxapp_start_server_bg, daemon=True).start()

    def _wxapp_start_server_bg(self):
        """后台线程入口，完成后恢复按钮状态"""
        try:
            self._wxapp_start_server()
        finally:
            if not self._wxapp_running:
                self.root.after(0, lambda: self.btn_wxapp_server.config(state="normal"))
            else:
                self.root.after(0, lambda: self.btn_wxapp_server.config(state="normal"))

    def _wxapp_start_server(self):
        """一键启动：自动安装pyngrok → 建立隧道 → 拿公网URL → 启动HTTP服务器"""
        if not self.wxapp_config.is_configured:
            self._auto_log("❌ 请先保存完整配置（CorpID/AgentID/Secret）"); return

        authtoken = self.wxapp_config.ngrok_authtoken
        if not authtoken:
            self._auto_log("❌ 请先填写 ngrok Token（免费注册 ngrok.com 获取）"); return

        self.root.after(0, lambda: self.lbl_wxapp_status.config(text="⏳ 启动中…", fg="#fa8c16"))

        # ── Step 1：确保 pyngrok 已安装 ──────────────────────────────────────
        try:
            import pyngrok
        except ImportError:
            self._auto_log("📦 pyngrok 未安装，正在自动安装（仅首次需要）…")
            import subprocess, sys
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "pyngrok", "--quiet", "--break-system-packages"])
            self._auto_log("✅ pyngrok 安装完成")

        from pyngrok import ngrok as _ngrok, conf as _ngrok_conf

        # ── Step 2：配置 authtoken ──────────────────────────────────────────
        _ngrok_conf.get_default().auth_token = authtoken

        # ── Step 3：建立 HTTP 隧道（自动处理已有隧道冲突）────────────────────
        def _start_tunnel():
            try:
                return _ngrok.connect(WXAPP_SERVER_PORT, "http")
            except Exception as e:
                err_str = str(e)
                # ERR_NGROK_334：已有同地址隧道在跑 → 先全部关掉再重试
                if "ERR_NGROK_334" in err_str or "already online" in err_str:
                    self._auto_log("⚠️ 检测到已有 ngrok 隧道，自动关闭后重试…")
                    _ngrok.kill()
                    import time as _t; _t.sleep(1)
                    return _ngrok.connect(WXAPP_SERVER_PORT, "http")
                raise

        try:
            tunnel = _start_tunnel()
            public_url = tunnel.public_url
            if not public_url.startswith("https"):
                public_url = public_url.replace("http://", "https://")
            callback_url = public_url + "/wx"
            self.wxapp_config.ngrok_url = public_url
            self.wxapp_config.save()
            self._auto_log("🌐 ngrok 隧道建立成功：" + public_url)
            self._auto_log("📋 企业微信后台「接收消息URL」填入：" + callback_url)
            self.root.after(0, lambda: self.wx5_callback_url.set(callback_url))
        except Exception as e:
            self._auto_log("❌ ngrok 隧道建立失败：" + str(e))
            self._auto_log("  请检查 Token 是否正确，或网络是否能访问 ngrok.com")
            self.root.after(0, lambda: self.lbl_wxapp_status.config(text="❌ 隧道失败", fg="#cf1322"))
            return

        # ── Step 4：启动本地 HTTP 服务器 ────────────────────────────────────
        app_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass

            def do_GET(self):
                """处理企业微信的URL验证请求"""
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                # 支持两种参数名：msg_signature 和 signature
                sig   = params.get("msg_signature", params.get("signature", [""]))[0]
                ts    = params.get("timestamp",     [""])[0]
                nonce = params.get("nonce",         [""])[0]
                echo  = params.get("echostr",       [""])[0]

                # 记录验证请求和请求头
                app_ref._auto_log(f"📥 收到验证请求：path={self.path}")
                app_ref._auto_log(f"   User-Agent: {self.headers.get('User-Agent', 'N/A')}")
                app_ref._auto_log(f"   所有请求头: {dict(self.headers)}")
                app_ref._auto_log(f"   timestamp={ts}, nonce={nonce[:10] if nonce else ''}...")

                # 验证签名
                if app_ref.wxapp_client.verify_signature(sig, ts, nonce, echo):
                    app_ref._auto_log("✅ 签名验证通过，返回echostr")
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(echo.encode('utf-8'))
                else:
                    app_ref._auto_log("❌ 签名验证失败！")
                    app_ref._auto_log(f"   期望的Token: {app_ref.wxapp_config.token}")
                    self.send_response(403)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(b'Signature verification failed')

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                self.send_response(200); self.end_headers()
                self.wfile.write(b"success")
                try:
                    root_el  = ET.fromstring(body.decode("utf-8"))
                    msg_type = (root_el.findtext("MsgType") or "").strip()
                    content  = (root_el.findtext("Content") or "").strip()
                    sender   = (root_el.findtext("FromUserName") or "").strip()
                except Exception:
                    return
                if msg_type != "text" or not content: return
                app_ref._auto_log("📨 收到指令 [" + sender + "]：" + content)
                app_ref.root.after(0, lambda: app_ref._wxapp_handle_cmd(sender, content))

        try:
            server = HTTPServer(("0.0.0.0", WXAPP_SERVER_PORT), Handler)
            self._wxapp_server  = server
            self._wxapp_running = True
            self._wxapp_server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            self._wxapp_server_thread.start()
            self._auto_log("✅ 本地服务已启动，端口 " + str(WXAPP_SERVER_PORT))
            self._auto_log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            self._auto_log("👉 现在去企业微信后台把上面的回调URL填入「接收消息」并保存，即可开始收发指令")
            self.root.after(0, lambda: (
                self.btn_wxapp_server.config(text="⏹ 停止服务", bg="#ff4d4f", fg="white"),
                self.lbl_wxapp_status.config(text="🟢 运行中", fg="#389e0d"),
            ))
        except Exception as e:
            self._auto_log("❌ 本地服务启动失败：" + str(e))

    def _wxapp_stop_server(self):
        # 关闭 HTTP 服务器
        if self._wxapp_server:
            self._wxapp_server.shutdown()
            self._wxapp_server = None
        # 关闭 ngrok 隧道
        try:
            from pyngrok import ngrok as _ngrok
            _ngrok.kill()
        except Exception:
            pass
        self._wxapp_running = False
        self._auto_log("⏹ 服务已停止，ngrok 隧道已关闭")
        self.root.after(0, lambda: (
            self.btn_wxapp_server.config(text="▶ 一键启动", bg="#52c41a", fg="black"),
            self.lbl_wxapp_status.config(text="⚪ 未启动", fg="#888"),
            self.wx5_callback_url.set("（启动后自动生成，粘贴到企业微信后台）"),
        ))

    def _wxapp_handle_cmd(self, sender: str, text: str):
        """在主线程执行指令，结果通过企业微信回复"""
        reply = self.wxapp_dispatcher.dispatch(sender, text)
        if reply:
            threading.Thread(
                target=lambda: self.wxapp_client.send_text(reply, to_user=sender),
                daemon=True
            ).start()
            self._auto_log(f"📤 回复 [{sender}]：{reply[:60]}{'…' if len(reply)>60 else ''}")

    def _register_wxapp_commands(self):
        """注册所有可用指令，绑定到 self 的数据和方法"""
        d = self.wxapp_dispatcher

        def cmd_help(sender, args):
            lines = [
                "🤖 转转调价助手 可用指令：",
                "",
                "  今日销售         — 今天各店成交汇总",
                "  库存查询 [型号]  — 查询在架机器数量和价格区间",
                "  触发调价         — 立即执行模块1自动调价",
                "  滞销预警         — 立即运行模块2检查滞销",
                "  AI [问题]        — 与AI助手对话",
                "  帮助 / ?         — 显示本说明",
                "",
                "💡 提示：直接发送任何问题，AI会自动回答",
            ]
            return "\n".join(lines)

        def cmd_today_sales(sender, args):
            """今日已售汇总：从本地缓存统计今天成交数据"""
            if self.engine is None or self.engine.df.empty:
                return "⚠️ 暂无本地成交数据，请先在「数据管理」页同步"
            df  = self.engine.df
            today = pd.Timestamp.now().normalize()
            sold_today = df[df["售出时间"] >= today]
            if sold_today.empty:
                return "📊 今日销售（" + today.strftime("%m/%d") + "）\n\n暂无成交记录"
            total_cnt   = len(sold_today)
            total_amt   = int(sold_today["最终售价"].sum())
            avg_price   = int(sold_today["最终售价"].mean())
            avg_hours   = round(sold_today["sales_hours"].mean(), 1) if "sales_hours" in sold_today.columns else "--"
            top_models  = sold_today["型号"].value_counts().head(3)
            model_lines = "\n".join("  · " + str(m) + "  " + str(c) + "台" for m, c in top_models.items())
            date_str    = today.strftime("%m/%d")  # FIX: 补充未定义的 date_str
            parts = [
                "📊 今日销售（" + date_str + "）",
                "━━━━━━━━━━━━━━",
                "成交：" + str(total_cnt) + " 台  合计 ¥" + "{:,}".format(total_amt),
                "均价：¥" + str(avg_price) + "  均动销：" + str(avg_hours) + "h",
                "",
                "热销型号：",
                model_lines,
            ]
            return "\n".join(parts)

        def cmd_stock_query(sender, args):
            """查询某型号在批量列表里的在架情况"""
            if not args:
                return "用法：库存查询 iPhone15Pro"
            if not self._batch_items:
                return "⚠️ 批量列表为空，请先在「批量调价」页拉取商品"
            kw = args.lower()
            matched = [it for it in self._batch_items
                       if kw in it.model.lower() and it.status_name in ("已上架", "在售")]
            if not matched:
                return f"⚠️ 「{args}」未找到在架商品（共搜索 {len(self._batch_items)} 件）"
            prices = [int(it.current_price) for it in matched if it.current_price]
            price_range = f"¥{min(prices)}~¥{max(prices)}" if prices else "--"
            by_cond: dict[str, int] = {}
            for it in matched:
                by_cond[it.condition] = by_cond.get(it.condition, 0) + 1
            cond_lines = "  " + "  ".join(f"{k}:{v}台" for k, v in by_cond.items())
            parts = [
                "📦 库存查询：" + args,
                "━━━━━━━━━━━━━━",
                "在架：" + str(len(matched)) + " 台  价格区间：" + price_range,
                "成色：" + cond_lines,  # FIX: cond_str → cond_lines
            ]
            return "\n".join(parts)

        def cmd_trigger_reprice(sender, args):
            """触发模块1自动调价"""
            if not self.wxapp_config.is_configured:
                return "⚠️ 请先配置自建应用"
            self._auto_log(f"📨 收到远程指令：触发调价（来自 {sender}）")
            threading.Thread(
                target=lambda: self._run_reprice_and_notify(sender),
                daemon=True
            ).start()
            return "✅ 调价任务已启动，完成后将回复结果…"

        def cmd_stale_warning(sender, args):
            """触发模块2滞销检查"""
            self._auto_log(f"📨 收到远程指令：滞销预警（来自 {sender}）")
            threading.Thread(
                target=lambda: self._run_stale_and_notify(sender),
                daemon=True
            ).start()
            return "✅ 滞销检查已启动，完成后将回复结果…"

        def cmd_ai_chat(sender, args):
            """AI助手对话"""
            if not args:
                return "💡 用法：AI [你的问题]\n例如：AI 分析当前库存\n或直接发送问题，我会自动识别"

            api_key = self.ai_api_key.get().strip() if hasattr(self, 'ai_api_key') else ""
            if not api_key:
                return "⚠️ AI功能未配置，请在软件中配置Claude API Key"

            self._auto_log(f"📨 收到AI对话请求（来自 {sender}）：{args}")

            # 在后台线程调用AI
            threading.Thread(
                target=lambda: self._run_ai_and_notify(sender, args),
                daemon=True
            ).start()
            return "🤖 AI正在思考中，请稍候..."

        d.register("帮助",   cmd_help,         "?", "？", "help")
        d.register("今日销售", cmd_today_sales, "销售数据", "今日数据", "销售")
        d.register("库存查询", cmd_stock_query, "库存", "查库存")
        d.register("触发调价", cmd_trigger_reprice, "调价", "改价")
        d.register("滞销预警", cmd_stale_warning,   "滞销", "预警")
        d.register("AI", cmd_ai_chat, "ai", "问AI", "智能助手")

        # 设置默认处理器：如果没有匹配的命令，当作AI对话
        d.set_default_handler(cmd_ai_chat)

    def _run_reprice_and_notify(self, to_user: str):
        """后台执行模块1调价，完成后推送结果给指令发送者"""
        try:
            self._auto_run_reprice()
            # 从日志缓冲里拿最后几行汇总
            log_tail = self._get_auto_log_tail(15)
            self.wxapp_client.send_text("✅ 调价完成\n\n" + log_tail, to_user=to_user)
        except Exception as e:
            self.wxapp_client.send_text("❌ 调价执行出错：" + str(e), to_user=to_user)

    def _run_stale_and_notify(self, to_user: str):
        """后台执行模块2滞销检查，完成后推送结果"""
        try:
            self._auto_run_stale()
            log_tail = self._get_auto_log_tail(15)
            self.wxapp_client.send_text("✅ 滞销检查完成\n\n" + log_tail, to_user=to_user)
        except Exception as e:
            self.wxapp_client.send_text("❌ 滞销检查出错：" + str(e), to_user=to_user)

    def _run_ai_and_notify(self, to_user: str, question: str):
        """后台调用AI，完成后推送结果"""
        try:
            import anthropic

            api_key = self.ai_api_key.get().strip() if hasattr(self, 'ai_api_key') else ""
            if not api_key:
                self.wxapp_client.send_text("⚠️ AI功能未配置", to_user=to_user)
                return

            client = anthropic.Anthropic(api_key=api_key)

            # 构建上下文
            context = self._build_ai_context()

            # 调用AI
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[
                    {"role": "user", "content": f"{context}\n\n用户问题：{question}"}
                ]
            )

            answer = response.content[0].text

            # 发送回复（如果太长则分段发送）
            if len(answer) > 1500:
                # 分段发送
                parts = [answer[i:i+1500] for i in range(0, len(answer), 1500)]
                for i, part in enumerate(parts):
                    header = f"🤖 AI回复 ({i+1}/{len(parts)}):\n\n" if len(parts) > 1 else "🤖 AI回复:\n\n"
                    self.wxapp_client.send_text(header + part, to_user=to_user)
                    if i < len(parts) - 1:
                        import time
                        time.sleep(1)  # 避免发送太快
            else:
                self.wxapp_client.send_text("🤖 AI回复:\n\n" + answer, to_user=to_user)

            self._auto_log(f"✅ AI回复已发送给 {to_user}")

        except ImportError:
            self.wxapp_client.send_text("❌ 缺少anthropic库，请先安装：pip install anthropic", to_user=to_user)
        except Exception as e:
            self.wxapp_client.send_text(f"❌ AI调用失败：{str(e)}", to_user=to_user)
            self._auto_log(f"❌ AI调用失败：{e}")

    def _get_auto_log_tail(self, n: int = 15) -> str:
        """取自动化日志最后n行"""
        try:
            content = self.auto_log.get("1.0", tk.END).strip()
            lines   = [l for l in content.splitlines() if l.strip()]
            return "\n".join(lines[-n:])
        except Exception:
            return ""

    def _wxwork_notify(self, text: str):
        """向企业微信推送一条文本消息，未配置 Webhook 则静默跳过"""
        webhook = self.auto4_webhook.get().strip() if hasattr(self, "auto4_webhook") else ""
        if not webhook:
            return
        try:
            resp = requests.post(webhook, json={
                "msgtype": "text",
                "text":    {"content": text},
            }, timeout=8)
            result = resp.json()
            if result.get("errcode") != 0:
                self._auto_log(f"⚠️ 企业微信推送失败：{result.get('errmsg', '')}")
        except Exception as e:
            self._auto_log(f"⚠️ 企业微信推送异常：{e}")

    # ════════════════════════════════════════════════════════════════════════
    # 模块4 实现：企业微信机器人销售播报
    # ════════════════════════════════════════════════════════════════════════
    def _save_webhook_config(self):
        """保存企业微信 Webhook 配置到文件"""
        config = {
            "webhook_url": self.auto4_webhook.get().strip(),
            "enabled": self.auto4_enabled.get(),
            "times": self.auto4_times.get().strip(),
        }
        try:
            with open("webhook_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self._auto_log("✅ 企业微信 Webhook 配置已保存")
            messagebox.showinfo("完成", "Webhook 配置已保存")
        except Exception as e:
            self._auto_log(f"❌ 保存配置失败: {e}")
            messagebox.showerror("错误", f"保存配置失败: {e}")

    def _load_webhook_config(self):
        """从文件加载企业微信 Webhook 配置"""
        try:
            if os.path.exists("webhook_config.json"):
                with open("webhook_config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                if hasattr(self, "auto4_webhook"):
                    self.auto4_webhook.set(config.get("webhook_url", ""))
                if hasattr(self, "auto4_enabled"):
                    self.auto4_enabled.set(config.get("enabled", False))
                if hasattr(self, "auto4_times"):
                    self.auto4_times.set(config.get("times", "09:00,12:00,18:00,21:00"))
                logger.info("加载企业微信 Webhook 配置成功")
        except Exception as e:
            logger.warning(f"加载 Webhook 配置失败: {e}")

    def _auto4_test_webhook(self):
        webhook = self.auto4_webhook.get().strip()
        if not webhook:
            self._auto_log("❌ 请先填写企业微信 Webhook URL"); return
        try:
            resp = requests.post(webhook, json={
                "msgtype": "text",
                "text":    {"content": "🤖 转转调价助手 - Webhook 连接测试成功 ✅"},
            }, timeout=8)
            result = resp.json()
            if result.get("errcode") == 0:
                self._auto_log("✅ 企业微信 Webhook 测试成功")
            else:
                self._auto_log(f"⚠️ 企业微信返回：{result.get('errmsg', resp.text[:120])}")
        except Exception as e:
            self._auto_log(f"❌ Webhook 测试失败：{e}")

    def _auto_run_feishu_report(self):
        webhook = self.auto4_webhook.get().strip()
        if not webhook:
            self._auto_log("❌ 模块4：未填写企业微信 Webhook URL"); return

        cookie = self._get_auto_cookie()
        if not cookie:
            self._auto_log("❌ 模块4：未选择操作店铺"); return

        if not self._batch_items:
            self._auto_log("❌ 模块4：暂无导入商品，请先在导入区输入质检码导入"); return

        svc = ImeiService(cookie)
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        # ── 拉取最新状态 ──────────────────────────────────────────────────
        qc_codes  = [i.qc_code for i in self._batch_items if i.qc_code]
        imeis     = [i.imei    for i in self._batch_items if i.imei and not i.qc_code]
        all_codes = qc_codes + imeis
        raw_map: dict[str, dict] = {}
        if all_codes:
            try:
                found_raw, _ = svc.fetch_by_codes(all_codes)
                for r in found_raw:
                    key = r.get("qcCode") or r.get("imei") or ""
                    if key:
                        raw_map[key] = r
            except Exception as e:
                self._auto_log(f"⚠️ 拉取最新状态失败，使用缓存数据：{e}")

        def get_raw(item: "BatchItem") -> dict:
            return raw_map.get(item.qc_code) or raw_map.get(item.imei) or {}

        def get_status(item: "BatchItem") -> str:
            r   = get_raw(item)
            st  = (r.get("state") or {}).get("status")
            return str(st) if st is not None else item.status_code

        # ── 分类统计 ──────────────────────────────────────────────────────
        on_sale_items  = []   # 在架（status==0）
        today_sold     = []   # 今日已售（status==80 且 soldTime 是今天）
        unlisted_items = []   # 未上架（status==60）

        for item in self._batch_items:
            r      = get_raw(item)
            status = get_status(item)
            sold_time = (r.get("lifecycleTimes") or {}).get("soldTime") or ""
            price  = (r.get("priceInfo") or {}).get("sellingPrice", 0) / 100 or item.current_price

            if status == "80" and sold_time.startswith(today_str):
                today_sold.append((item, price, sold_time))
            elif status == "0":
                on_sale_items.append((item, price))
            elif status == "60":
                unlisted_items.append(item)

        # ── 组装报文 ──────────────────────────────────────────────────────
        lines: list[str] = []
        lines.append(f"【转转销售播报】{now.strftime('%m月%d日 %H:%M')}")
        lines.append("")

        # 概览摘要
        total_gmv = sum(p for _, p, _ in today_sold)
        lines.append(f"📌 监控商品 {len(self._batch_items)} 件")
        lines.append(f"🟢 当前在架：{len(on_sale_items)} 件")
        lines.append(f"🛒 今日已售：{len(today_sold)} 件  GMV ¥{total_gmv:,.0f}")
        lines.append(f"🔖 待定价上架：{len(unlisted_items)} 件")
        lines.append("─" * 28)

        # ── 今日已售明细 ──────────────────────────────────────────────────
        if self.auto4_inc_sold.get():
            lines.append(f"🛒 今日已售明细（{len(today_sold)} 件）")
            if today_sold:
                today_sold.sort(key=lambda x: x[2], reverse=True)
                for item, price, st in today_sold[:8]:
                    t = st[11:16] if len(st) >= 16 else ""
                    lines.append(
                        f"  ✅ {t}  [{item.qc_code or item.imei}]  "
                        f"{item.model} {item.condition} {item.capacity}  ¥{price:.0f}"
                    )
                if len(today_sold) > 8:
                    lines.append(f"  ……共 {len(today_sold)} 件")
            else:
                lines.append("  暂无今日成交")

        # ── 在架商品概况 ──────────────────────────────────────────────────
        if self.auto4_inc_on_sale.get() and on_sale_items:
            lines.append("")
            avg_price = int(sum(p for _, p in on_sale_items) / len(on_sale_items))
            lines.append(f"🟢 当前在架明细（{len(on_sale_items)} 件，均价 ¥{avg_price}）")
            top = sorted(on_sale_items, key=lambda x: -x[1])[:5]
            for item, price in top:
                lines.append(
                    f"  · [{item.qc_code or item.imei}]  "
                    f"{item.model} {item.condition} {item.capacity}  ¥{int(price)}"
                )
            if len(on_sale_items) > 5:
                lines.append(f"  ……共 {len(on_sale_items)} 件")

        # ── 滞销预警 ──────────────────────────────────────────────────────
        if self.auto4_inc_stale.get():
            lines.append("")
            lines.append("─" * 28)
            try:
                days1 = int(self.auto2_days1.get()) if self.auto2_enabled.get() else 3
            except Exception:
                days1 = 3

            stale_warn = []
            for item, price in on_sale_items:
                r = get_raw(item)
                put_on = (r.get("lifecycleTimes") or {}).get("putOnMartTime")
                if not put_on:
                    continue
                try:
                    age = (now - datetime.strptime(put_on, "%Y-%m-%d %H:%M:%S")).days
                except Exception:
                    continue
                if age >= days1:
                    stale_warn.append((age, item, price))

            stale_warn.sort(key=lambda x: -x[0])
            lines.append(f"⏳ 滞销预警（>{days1}天未售）：{len(stale_warn)} 件")
            for age, item, price in stale_warn[:6]:
                lines.append(
                    f"  ⚠️ 库龄{age}天  [{item.qc_code or item.imei}]  "
                    f"{item.model} {item.condition} {item.capacity}  ¥{int(price)}"
                )
            if len(stale_warn) > 6:
                lines.append(f"  ……共 {len(stale_warn)} 件")

        # ── 待定价上架 ────────────────────────────────────────────────────
        if self.auto4_inc_unlisted.get() and unlisted_items:
            lines.append("")
            lines.append("─" * 28)
            lines.append(f"🔖 待定价上架（{len(unlisted_items)} 件）")
            for item in unlisted_items[:5]:
                lines.append(
                    f"  · [{item.qc_code or item.imei}]  "
                    f"{item.model} {item.condition} {item.capacity}"
                )
            if len(unlisted_items) > 5:
                lines.append(f"  ……共 {len(unlisted_items)} 件")

        # ── 推送企业微信 ──────────────────────────────────────────────────
        text = "\n".join(lines)
        self._auto_log("📤 推送企业微信播报...")
        try:
            resp = requests.post(webhook, json={
                "msgtype": "text",
                "text":    {"content": text},
            }, timeout=8)
            result = resp.json()
            if result.get("errcode") == 0:
                self._auto_log("✅ 企业微信播报推送成功")
            else:
                self._auto_log(f"⚠️ 企业微信返回：{result.get('errmsg', resp.text[:120])}")
        except Exception as e:
            self._auto_log(f"❌ 推送失败：{e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  📊 数据分析扩展模块（独立测试，不影响现有架构）
#  包含：
#    1. ErpSoldImporter  — ERP成交记录接入定价引擎
#    2. MarketScanner    — 转转市场实时行情扫描
#    3. SoldAnalyzer     — 动销时长分层分析
#  入口：DataLabWindow（独立浮窗，主菜单「🔬 数据实验室」按钮打开）
# ═══════════════════════════════════════════════════════════════════════════════

    def _build_tab_smart_reprice(self, nb: ttk.Notebook):
        """智能调价看板：整合模块1/2/3，提供可视化确认界面和日志查询"""
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 🎯 智能调价看板 ")

        # 初始化调价候选列表
        self._reprice_candidates: list[dict] = []
        self._reprice_log_file = "reprice_log.csv"

        # 豁免区：存储不需要自动调价的商品 product_id
        self._exempt_items: set[str] = set()
        self._exempt_file = "exempt_list.json"
        self._load_exempt_list()

        # ── 顶部控制栏 ────────────────────────────────────────────────────────
        frm_ctrl = tk.LabelFrame(tab, text=" 操作控制 ",
                                font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=8)
        frm_ctrl.pack(fill=tk.X, padx=12, pady=(12, 4))

        row1 = tk.Frame(frm_ctrl); row1.pack(fill=tk.X, pady=4)
        tk.Label(row1, text="操作店铺:", font=self.FONT_DEFAULT).pack(side=tk.LEFT)
        self.combo_smart_account = ttk.Combobox(row1, state="readonly",
                                               font=self.FONT_DEFAULT, width=18)
        self.combo_smart_account.pack(side=tk.LEFT, padx=(6, 16))
        ttk.Button(row1, text="🔄", width=3,
                  command=self._refresh_smart_account_list).pack(side=tk.LEFT, padx=(0, 16))

        tk.Button(row1, text="🔍 扫描需要调价的商品",
                 command=self._scan_reprice_candidates,
                 font=self.FONT_DEFAULT, bg="#1890ff", fg="black").pack(side=tk.LEFT, padx=4)

        tk.Button(row1, text="📋 查看调价日志",
                 command=self._open_reprice_log_window,
                 font=self.FONT_DEFAULT, bg="#595959", fg="white").pack(side=tk.LEFT, padx=4)

        # ── 统计卡片 ──────────────────────────────────────────────────────────
        frm_stats = tk.LabelFrame(tab, text=" 调价统计 ",
                                 font=self.FONT_BOLD, fg="#d46b08", padx=10, pady=6)
        frm_stats.pack(fill=tk.X, padx=12, pady=(0, 4))

        stats_row = tk.Frame(frm_stats); stats_row.pack(fill=tk.X)
        self._smart_stats_labels = {}
        for key, label, color in [("total", "待调价", "#333"),
                                   ("module1", "建议价调价", "#1890ff"),
                                   ("module2", "滞销降价", "#fa8c16"),
                                   ("module3", "未上架定价", "#52c41a")]:
            col = tk.Frame(stats_row); col.pack(side=tk.LEFT, padx=18)
            tk.Label(col, text=label, font=("PingFang SC", 11), fg="#888").pack()
            lbl = tk.Label(col, text="0", font=("PingFang SC", 20, "bold"), fg=color)
            lbl.pack()
            self._smart_stats_labels[key] = lbl

        # ── 操作按钮行 ────────────────────────────────────────────────────────
        frm_actions = tk.Frame(tab); frm_actions.pack(fill=tk.X, padx=12, pady=4)

        tk.Button(frm_actions, text="✅ 全选", font=self.FONT_DEFAULT,
                 command=self._smart_select_all).pack(side=tk.LEFT, padx=2)
        tk.Button(frm_actions, text="❌ 全不选", font=self.FONT_DEFAULT,
                 command=self._smart_deselect_all).pack(side=tk.LEFT, padx=2)
        tk.Button(frm_actions, text="🔽 仅选降价", font=self.FONT_DEFAULT,
                 command=self._smart_select_lower).pack(side=tk.LEFT, padx=2)

        tk.Button(frm_actions, text="🚀 一键全部调价", font=("PingFang SC", 12, "bold"),
                 bg="#ff4d4f", fg="white",
                 command=self._smart_reprice_all).pack(side=tk.RIGHT, padx=4)
        tk.Button(frm_actions, text="✅ 批量调价���勾选项）", font=("PingFang SC", 12, "bold"),
                 bg="#52c41a", fg="black",
                 command=self._smart_reprice_checked).pack(side=tk.RIGHT, padx=4)
        tk.Button(frm_actions, text="🛡️ 加入豁免区", font=("PingFang SC", 11),
                 bg="#faad14", fg="black",
                 command=self._add_to_exempt).pack(side=tk.RIGHT, padx=4)

        # ── 商品列表（Treeview）─────────────────────────────────────────────
        frm_tree = tk.Frame(tab); frm_tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        cols = ("check", "module", "qc_code", "model", "condition", "capacity", "color",
                "current_price", "suggested_price", "diff", "settle_price", "reason")
        self.smart_tree = ttk.Treeview(frm_tree, columns=cols, show="headings", height=15)

        headers = {"check": "✓", "module": "类型", "qc_code": "质检码",
                  "model": "型号", "condition": "成色", "capacity": "容量", "color": "颜色",
                  "current_price": "当前价", "suggested_price": "建议价",
                  "diff": "调价幅度", "settle_price": "预计到手", "reason": "调价原因"}
        widths = {"check": 40, "module": 80, "qc_code": 100, "model": 150,
                 "condition": 80, "capacity": 60, "color": 80,
                 "current_price": 70, "suggested_price": 70, "diff": 80,
                 "settle_price": 80, "reason": 150}

        for col in cols:
            self.smart_tree.heading(col, text=headers[col])
            self.smart_tree.column(col, width=widths[col], anchor="center")

        sb = ttk.Scrollbar(frm_tree, orient="vertical", command=self.smart_tree.yview)
        self.smart_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.smart_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 点击第一列切换勾选状态
        self.smart_tree.bind("<Button-1>", self._smart_tree_toggle_check)

        # ── 豁免区面板 ────────────────────────────────────────────────────────
        frm_exempt = tk.LabelFrame(tab, text=" 🛡️ 豁免区（不自动调价） ",
                                  font=self.FONT_BOLD, fg="#fa8c16", padx=8, pady=6)
        frm_exempt.pack(fill=tk.X, padx=12, pady=4)

        frm_exempt_ctrl = tk.Frame(frm_exempt)
        frm_exempt_ctrl.pack(fill=tk.X, pady=(0, 4))
        tk.Label(frm_exempt_ctrl, text="豁免区商品不会被自动调价", font=self.FONT_DEFAULT, fg="#888").pack(side=tk.LEFT)
        tk.Button(frm_exempt_ctrl, text="移除豁免", bg="#ff4d4f", fg="white",
                 command=self._remove_from_exempt).pack(side=tk.RIGHT, padx=2)
        tk.Button(frm_exempt_ctrl, text="清空豁免区", bg="#d9d9d9",
                 command=self._clear_exempt).pack(side=tk.RIGHT, padx=2)

        exempt_cols = ("qc_code", "model", "condition", "capacity", "color", "current_price")
        self.exempt_tree = ttk.Treeview(frm_exempt, columns=exempt_cols, show="headings", height=5)
        exempt_headers = {"qc_code": "质检码", "model": "型号", "condition": "成色",
                          "capacity": "容量", "color": "颜色", "current_price": "当前价"}
        exempt_widths = {"qc_code": 100, "model": 150, "condition": 80,
                         "capacity": 60, "color": 80, "current_price": 70}
        for col in exempt_cols:
            self.exempt_tree.heading(col, text=exempt_headers[col])
            self.exempt_tree.column(col, width=exempt_widths[col], anchor="center")
        self.exempt_tree.pack(fill=tk.X, pady=2)


        # ── 底部状态栏 ────────────────────────────────────────────────────────
        self.lbl_smart_status = tk.Label(tab, text="", font=self.FONT_DEFAULT, fg="#888")
        self.lbl_smart_status.pack(pady=4)

        self._refresh_smart_account_list()
        self._refresh_exempt_tree()  # 初始化时显示豁免区数据

    # ── 智能调价看板：支持方法 ────────────────────────────────────────────────
    def _refresh_smart_account_list(self):
        """刷新店铺列表"""
        names = [a.name for a in self.account_mgr.accounts]
        self.combo_smart_account["values"] = names
        if names and not self.combo_smart_account.get():
            self.combo_smart_account.set(names[0])

    def _get_smart_cookie(self):
        """获取当前选择的店铺cookie"""
        name = self.combo_smart_account.get()
        for a in self.account_mgr.accounts:
            if a.name == name:
                return a.cookie
        return None

    def _load_exempt_list(self):
        """从文件加载豁免列表"""
        try:
            if os.path.exists(self._exempt_file):
                with open(self._exempt_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._exempt_items = set(data.get("exempt_ids", []))
                    logger.info(f"加载豁免列表：{len(self._exempt_items)} 件商品")
            else:
                logger.info("豁免列表文件不存在，使用空列表")
        except Exception as e:
            logger.warning(f"加载豁免列表失败: {e}")
            self._exempt_items = set()

    def _save_exempt_list(self):
        """保存豁免列表到文件"""
        try:
            with open(self._exempt_file, "w", encoding="utf-8") as f:
                json.dump({"exempt_ids": list(self._exempt_items)}, f, ensure_ascii=False, indent=2)
            logger.info(f"保存豁免列表：{len(self._exempt_items)} 件商品")
        except Exception as e:
            logger.warning(f"保存豁免列表失败: {e}")

    def _refresh_exempt_tree(self):
        """刷新豁免区列表显示"""
        self.exempt_tree.delete(*self.exempt_tree.get_children())
        for item in self._batch_items:
            if item.product_id in self._exempt_items:
                self.exempt_tree.insert("", tk.END, values=(
                    item.qc_code,
                    item.model,
                    item.condition,
                    item.capacity,
                    item.color,
                    int(item.current_price) if item.current_price else "--",
                ), tags=(item.product_id,))

    def _add_to_exempt(self):
        """将选中的候选商品加入豁免区"""
        checked = [c for c in self._reprice_candidates if c["checked"]]
        if not checked:
            messagebox.showwarning("提示", "请先勾选需要加入豁免区的商品")
            return

        for cand in checked:
            item = cand["item"]
            self._exempt_items.add(item.product_id)

        self._save_exempt_list()
        self._reprice_candidates = [c for c in self._reprice_candidates if not c["checked"]]
        self._render_smart_tree()
        self._refresh_exempt_tree()
        self._auto_log(f"✅ 已将 {len(checked)} 件商品加入豁免区")
        messagebox.showinfo("完成", f"已将 {len(checked)} 件商品加入豁免区")

    def _remove_from_exempt(self):
        """将选中的商品从豁免区移除"""
        selection = self.exempt_tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择需要移除的商品")
            return

        removed = 0
        for sel in selection:
            tags = self.exempt_tree.item(sel, "tags")
            if tags:
                product_id = tags[0]
                if product_id in self._exempt_items:
                    self._exempt_items.remove(product_id)
                    removed += 1

        self._save_exempt_list()
        self._refresh_exempt_tree()
        self._auto_log(f"✅ 已将 {removed} 件商品移出豁免区")

        # 自动重新扫描，让移出的商品出现在调价候选列表
        if removed > 0:
            self._scan_reprice_candidates()

    def _clear_exempt(self):
        """清空豁免区"""
        if not self._exempt_items:
            messagebox.showinfo("提示", "豁免区已经是空的")
            return

        if not messagebox.askyesno("确认", f"确定清空豁免区的 {len(self._exempt_items)} 件商品？"):
            return

        count = len(self._exempt_items)
        self._exempt_items.clear()
        self._save_exempt_list()
        self._refresh_exempt_tree()
        self._auto_log(f"✅ 已清空豁免区 {count} 件商品")

        # 自动重新扫描，让清空的商品出现在调价候选列表
        self._scan_reprice_candidates()

    def _scan_reprice_candidates(self):
        """扫描所有需要调价的商品"""
        cookie = self._get_smart_cookie()
        if not cookie:
            messagebox.showwarning("提示", "请先选择操作店铺")
            return
        if self.engine is None:
            messagebox.showwarning("提示", "请先在「数据管理」页同步历史成交数据")
            return
        if not self._batch_items:
            messagebox.showwarning("提示", "请先在「批量调价」或「自动化」页导入商品")
            return

        self.lbl_smart_status.config(text="🔍 正在扫描需要调价的商品...", fg="#1890ff")
        self._reprice_candidates.clear()

        def _worker():
            try:
                # 先统计已上架商品的实际费率（用于推算未上架商品到手价）
                fee_rates = []
                for item in self._batch_items:
                    if item.settle_price and item.current_price and item.current_price > 0:
                        rate = 1 - (item.settle_price / item.current_price)
                        if 0 < rate < 0.2:  # 合理范围内的费率（0-20%）
                            fee_rates.append(rate)
                avg_fee_rate = sum(fee_rates) / len(fee_rates) if fee_rates else PLATFORM_FEE_RATE

                # 模块1：已上架商品按建议价调价（排除豁免区）
                module1_items = [i for i in self._batch_items
                               if (str(i.status_code) == "0" or i.status_name in ("已上架", "在售"))
                               and i.product_id not in self._exempt_items]
                for item in module1_items:
                    result = self.engine.query(item.model, item.condition,
                                             item.capacity, item.color, "未知")
                    if result and result.suggested_price > 0:
                        diff = result.suggested_price - int(item.current_price)
                        if diff != 0:  # 只添加需要调价的
                            self._reprice_candidates.append({
                                "module": "建议价调价",
                                "qc_code": item.qc_code,
                                "model": item.model,
                                "condition": item.condition,
                                "capacity": item.capacity,
                                "color": item.color,
                                "current_price": int(item.current_price),
                                "suggested_price": result.suggested_price,
                                "diff": diff,
                                "settle_price": round(result.suggested_price * (1 - (1 - item.settle_price / item.current_price))) if item.settle_price and item.current_price else round(result.suggested_price * (1 - PLATFORM_FEE_RATE) - STATION_SERVICE_FEE),
                                "reason": f"历史数据建议价",
                                "checked": diff < 0,  # 默认勾选降价项
                                "item": item,
                            })

                # 模块2：滞销商品降价
                try:
                    days1 = int(self.auto2_days1.get()) if hasattr(self, "auto2_days1") else 7
                    days2 = int(self.auto2_days2.get()) if hasattr(self, "auto2_days2") else 14
                except:
                    days1, days2 = 7, 14

                svc = ImeiService(cookie)
                qc_codes = [i.qc_code for i in module1_items if i.qc_code]
                raw_map = {}
                if qc_codes:
                    try:
                        found, _ = svc.fetch_by_codes(qc_codes)
                        raw_map = {r.get("qcCode", ""): r for r in found}
                    except:
                        pass

                now = datetime.now()
                for item in module1_items:
                    raw = raw_map.get(item.qc_code, {})
                    put_on = (raw.get("lifecycleTimes") or {}).get("putOnMartTime")
                    if not put_on:
                        continue
                    try:
                        age_days = (now - datetime.strptime(put_on, "%Y-%m-%d %H:%M:%S")).days
                    except:
                        continue

                    if age_days >= days1:
                        result = self.engine.query(item.model, item.condition,
                                                 item.capacity, item.color, "未知")
                        if result and result.suggested_price > 0:
                            if age_days >= days2:
                                new_price = int(result.suggested_price * 0.95)  # 阶段2降5%
                                tier = 2
                            else:
                                new_price = result.suggested_price
                                tier = 1

                            if new_price < int(item.current_price):
                                # 检查是否已经在模块1中添加过
                                exists = any(c["qc_code"] == item.qc_code
                                           for c in self._reprice_candidates)
                                if not exists:
                                    self._reprice_candidates.append({
                                        "module": f"滞销降价T{tier}",
                                        "qc_code": item.qc_code,
                                        "model": item.model,
                                        "condition": item.condition,
                                        "capacity": item.capacity,
                                        "color": item.color,
                                        "current_price": int(item.current_price),
                                        "suggested_price": new_price,
                                        "diff": new_price - int(item.current_price),
                                        "settle_price": round(new_price * (1 - (1 - item.settle_price / item.current_price))) if item.settle_price and item.current_price else round(new_price * (1 - PLATFORM_FEE_RATE) - STATION_SERVICE_FEE),
                                        "reason": f"库龄{age_days}天",
                                        "checked": True,
                                        "item": item,
                                    })

                # 模块3：未上架商品定价（排除豁免区）
                svc = ImeiService(cookie)
                module3_items = [i for i in self._batch_items
                               if (str(i.status_code) == "60" or i.status_name == "未上架")
                               and i.product_id not in self._exempt_items]
                for item in module3_items:
                    if item.suggested_price > 0:  # 已经有建议价
                        # 直接从转转 API 获取实时到手价
                        real_settle = svc.fetch_settle_price(item.product_id) if item.product_id else None
                        if real_settle:
                            settle = int(real_settle)
                        else:
                            # API 获取失败，用统计费率推算
                            settle = round(item.suggested_price * (1 - avg_fee_rate))

                        self._reprice_candidates.append({
                            "module": "未上架定价",
                            "qc_code": item.qc_code,
                            "model": item.model,
                            "condition": item.condition,
                            "capacity": item.capacity,
                            "color": item.color,
                            "current_price": 0,
                            "suggested_price": item.suggested_price,
                            "diff": item.suggested_price,
                            "settle_price": settle,
                            "reason": "首次定价",
                            "checked": True,
                            "item": item,
                        })

                self.root.after(0, self._render_smart_tree)
                self.root.after(0, self._refresh_exempt_tree)
                self.root.after(0, lambda: self.lbl_smart_status.config(
                    text=f"✅ 扫描完成，找到 {len(self._reprice_candidates)} 件需要调价的商品（豁免 {len(self._exempt_items)} 件）",
                    fg="#389e0d"))

            except Exception as e:
                self.root.after(0, lambda: self.lbl_smart_status.config(
                    text=f"❌ 扫描失败：{e}", fg="#cf1322"))

        threading.Thread(target=_worker, daemon=True).start()

    def _render_smart_tree(self):
        """渲染调价候选列表"""
        self.smart_tree.delete(*self.smart_tree.get_children())

        # 统计
        stats = {"total": len(self._reprice_candidates),
                "module1": 0, "module2": 0, "module3": 0}

        for idx, cand in enumerate(self._reprice_candidates):
            check_mark = "✓" if cand["checked"] else ""
            diff_str = f"{cand['diff']:+d}"
            diff_color = "#52c41a" if cand["diff"] < 0 else "#ff4d4f"

            self.smart_tree.insert("", tk.END, iid=str(idx), values=(
                check_mark,
                cand["module"],
                cand["qc_code"],
                cand["model"],
                cand["condition"],
                cand["capacity"],
                cand["color"],
                cand["current_price"],
                cand["suggested_price"],
                diff_str,
                cand["settle_price"],
                cand["reason"],
            ), tags=(f"row_{idx}",))

            # 统计
            if "建议价" in cand["module"]:
                stats["module1"] += 1
            elif "滞销" in cand["module"]:
                stats["module2"] += 1
            elif "未上架" in cand["module"]:
                stats["module3"] += 1

        # 更新统计卡片
        for key, lbl in self._smart_stats_labels.items():
            lbl.config(text=str(stats.get(key, 0)))

    def _smart_tree_toggle_check(self, event):
        """点击第一列切换勾选状态"""
        col = self.smart_tree.identify_column(event.x)
        row = self.smart_tree.identify_row(event.y)
        if col == "#1" and row:  # 第一列
            idx = int(row)
            if idx < len(self._reprice_candidates):
                self._reprice_candidates[idx]["checked"] = not self._reprice_candidates[idx]["checked"]
                self._render_smart_tree()

    def _smart_select_all(self):
        """全选"""
        for cand in self._reprice_candidates:
            cand["checked"] = True
        self._render_smart_tree()

    def _smart_deselect_all(self):
        """全不选"""
        for cand in self._reprice_candidates:
            cand["checked"] = False
        self._render_smart_tree()

    def _smart_select_lower(self):
        """仅选降价项"""
        for cand in self._reprice_candidates:
            cand["checked"] = cand["diff"] < 0
        self._render_smart_tree()

    def _smart_reprice_all(self):
        """一键全部调价"""
        if not self._reprice_candidates:
            messagebox.showwarning("提示", "没有需要调价的商品")
            return
        if not messagebox.askyesno("确认", f"确定对全部 {len(self._reprice_candidates)} 件商品执行调价？"):
            return
        # 全选后执行
        for cand in self._reprice_candidates:
            cand["checked"] = True
        self._smart_reprice_checked()

    def _smart_reprice_checked(self):
        """批量调价（勾选项）"""
        checked = [c for c in self._reprice_candidates if c["checked"]]
        if not checked:
            messagebox.showwarning("提示", "请先勾选需要调价的商品")
            return

        cookie = self._get_smart_cookie()
        if not cookie:
            messagebox.showwarning("提示", "请先选择操作店铺")
            return

        if not messagebox.askyesno("确认", f"确定对勾选的 {len(checked)} 件商品执行调价？"):
            return

        self.lbl_smart_status.config(text=f"🔄 正在调价 {len(checked)} 件商品...", fg="#1890ff")
        self._auto_log(f"🎯 智能调价：开始调价 {len(checked)} 件商品")

        def _worker():
            svc = ImeiService(cookie)
            success = fail = 0
            logs = []

            total = len(checked)
            current = 0
            for cand in checked:
                item = cand["item"]
                current += 1
                progress_text = f"🔄 正在调价 {current}/{total} - {item.qc_code} {item.model}"
                self.root.after(0, lambda t=progress_text: self.lbl_smart_status.config(text=t, fg="#1890ff"))

                pd_obj = ProductDetail(
                    product_id=item.product_id, sku_id="", group_key="",
                    title=item.title, model=item.model, condition=item.condition,
                    capacity=item.capacity, color=item.color,
                    current_price=item.current_price, settle_price=0,
                    status=item.status_code,
                )
                try:
                    svc.update_price(pd_obj, float(cand["suggested_price"]))
                    item.current_price = float(cand["suggested_price"])

                    # 获取实际到手价
                    real_settle = svc.fetch_settle_price(item.product_id)
                    if real_settle:
                        settle_price = int(real_settle)
                    else:
                        settle_price = cand["settle_price"]

                    # 记录日志
                    log_entry = {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "shop": self.combo_smart_account.get(),
                        "qc_code": cand["qc_code"],
                        "model": cand["model"],
                        "condition": cand["condition"],
                        "capacity": cand["capacity"],
                        "color": cand["color"],
                        "old_price": cand["current_price"],
                        "new_price": cand["suggested_price"],
                        "diff": cand["diff"],
                        "settle_price": settle_price,
                        "module": cand["module"],
                        "reason": cand["reason"],
                        "status": "成功",
                    }
                    logs.append(log_entry)
                    success += 1

                    # 输出到日志
                    diff_arrow = "↓" if cand["diff"] < 0 else "↑"
                    self.root.after(0, lambda m=item.model, q=item.qc_code, o=cand["current_price"], n=cand["suggested_price"], d=cand["diff"], a=diff_arrow:
                        self._auto_log(f"  ✅ [{q}] {m} ¥{o}→¥{n} ({a}{abs(d)})"))

                except Exception as e:
                    log_entry = {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "shop": self.combo_smart_account.get(),
                        "qc_code": cand["qc_code"],
                        "model": cand["model"],
                        "condition": cand["condition"],
                        "capacity": cand["capacity"],
                        "color": cand["color"],
                        "old_price": cand["current_price"],
                        "new_price": cand["suggested_price"],
                        "diff": cand["diff"],
                        "settle_price": 0,
                        "module": cand["module"],
                        "reason": cand["reason"],
                        "status": f"失败: {e}",
                    }
                    logs.append(log_entry)
                    fail += 1

                    # 输出到日志
                    self.root.after(0, lambda q=cand["qc_code"], m=cand["model"], err=str(e):
                        self._auto_log(f"  ❌ [{q}] {m} 调价失败: {err}"))

                import time
                time.sleep(1)

            # 保存日志
            self._save_reprice_logs(logs)

            # 从候选列表中移除已调价的
            self._reprice_candidates = [c for c in self._reprice_candidates if not c["checked"]]

            self.root.after(0, self._render_smart_tree)
            self.root.after(0, lambda: self.lbl_smart_status.config(
                text=f"✅ 调价完成：成功 {success} 件，失败 {fail} 件",
                fg="#389e0d" if fail == 0 else "#fa8c16"))
            self.root.after(0, lambda s=success, f=fail:
                self._auto_log(f"🎯 智能调价完成：✅ 成功 {s} 件，❌ 失败 {f} 件"))

        threading.Thread(target=_worker, daemon=True).start()

    def _save_reprice_logs(self, logs: list[dict]):
        """保存调价日志到CSV"""
        import csv
        import os

        file_exists = os.path.exists(self._reprice_log_file)
        with open(self._reprice_log_file, "a", newline="", encoding="utf-8-sig") as f:
            fieldnames = ["timestamp", "shop", "qc_code", "model", "condition",
                         "capacity", "color", "old_price", "new_price", "diff",
                         "settle_price", "module", "reason", "status"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerows(logs)

    def _open_reprice_log_window(self):
        """打开调价日志查询窗口"""
        import os
        if not os.path.exists(self._reprice_log_file):
            messagebox.showinfo("提示", "暂无调价日志")
            return

        win = tk.Toplevel(self.root)
        win.title("📋 调价日志查询")
        win.geometry("1200x600")

        # 顶部工具栏
        toolbar = tk.Frame(win, bg="#f0f0f0", pady=6)
        toolbar.pack(fill=tk.X)

        tk.Label(toolbar, text="搜索:", font=self.FONT_DEFAULT, bg="#f0f0f0").pack(side=tk.LEFT, padx=(10, 4))
        search_var = tk.StringVar()
        search_entry = ttk.Entry(toolbar, textvariable=search_var, font=self.FONT_DEFAULT, width=30)
        search_entry.pack(side=tk.LEFT, padx=4)

        def _search():
            keyword = search_var.get().strip().lower()
            _load_logs(keyword)

        tk.Button(toolbar, text="🔍 搜索", font=self.FONT_DEFAULT,
                 command=_search).pack(side=tk.LEFT, padx=4)
        tk.Button(toolbar, text="🔄 刷新", font=self.FONT_DEFAULT,
                 command=lambda: _load_logs()).pack(side=tk.LEFT, padx=4)

        # 日志表格
        frm_tree = tk.Frame(win); frm_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        cols = ("timestamp", "shop", "qc_code", "model", "condition", "capacity", "color",
                "old_price", "new_price", "diff", "settle_price", "module", "reason", "status")
        log_tree = ttk.Treeview(frm_tree, columns=cols, show="headings", height=20)

        headers = {"timestamp": "时间", "shop": "店铺", "qc_code": "质检码",
                  "model": "型号", "condition": "成色", "capacity": "容量", "color": "颜色",
                  "old_price": "原价", "new_price": "新价", "diff": "调价幅度",
                  "settle_price": "到手价", "module": "类型", "reason": "原因", "status": "状态"}
        widths = {"timestamp": 140, "shop": 80, "qc_code": 100, "model": 120,
                 "condition": 80, "capacity": 60, "color": 80,
                 "old_price": 60, "new_price": 60, "diff": 70,
                 "settle_price": 70, "module": 90, "reason": 100, "status": 80}

        for col in cols:
            log_tree.heading(col, text=headers[col])
            log_tree.column(col, width=widths[col], anchor="center")

        sb = ttk.Scrollbar(frm_tree, orient="vertical", command=log_tree.yview)
        log_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        log_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _load_logs(keyword=""):
            import csv
            log_tree.delete(*log_tree.get_children())
            with open(self._reprice_log_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                logs = list(reader)
                logs.reverse()  # 最新的在前

                for log in logs:
                    if keyword:
                        searchable = f"{log.get('qc_code', '')} {log.get('model', '')} {log.get('condition', '')}".lower()
                        if keyword not in searchable:
                            continue

                    log_tree.insert("", tk.END, values=(
                        log.get("timestamp", ""),
                        log.get("shop", ""),
                        log.get("qc_code", ""),
                        log.get("model", ""),
                        log.get("condition", ""),
                        log.get("capacity", ""),
                        log.get("color", ""),
                        log.get("old_price", ""),
                        log.get("new_price", ""),
                        log.get("diff", ""),
                        log.get("settle_price", ""),
                        log.get("module", ""),
                        log.get("reason", ""),
                        log.get("status", ""),
                    ))

        _load_logs()

    # ══════════════════════════════════════════════════════════════════════════
    # AI 智能助手
    # ══════════════════════════════════════════════════════════════════════════
    def _build_tab_ai_assistant(self, nb: ttk.Notebook):
        """AI智能助手：集成Claude API，提供智能定价分析和数据查询"""
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 🤖 AI助手 ")

        # API配置区
        frm_config = tk.LabelFrame(tab, text=" API配置 ", font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=10)
        frm_config.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(frm_config, text="Claude API Key:", font=self.FONT_DEFAULT).grid(row=0, column=0, sticky=tk.W, pady=5)
        self.ai_api_key = tk.Entry(frm_config, width=50, font=self.FONT_DEFAULT, show="*")
        self.ai_api_key.grid(row=0, column=1, padx=5, pady=5)

        ttk.Button(frm_config, text="保存配置", command=self._save_ai_config).grid(row=0, column=2, padx=5)
        ttk.Button(frm_config, text="测试连接", command=self._test_ai_connection).grid(row=0, column=3, padx=5)

        # 功能选择区
        frm_func = tk.LabelFrame(tab, text=" 功能选择 ", font=self.FONT_BOLD, fg="#52c41a", padx=10, pady=10)
        frm_func.pack(fill=tk.X, padx=10, pady=10)

        btn_row = tk.Frame(frm_func)
        btn_row.pack(fill=tk.X, pady=5)

        tk.Button(btn_row, text="📊 智能定价分析", font=self.FONT_DEFAULT, bg="#1890ff", fg="white",
                 command=self._ai_pricing_analysis).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_row, text="📈 市场趋势分析", font=self.FONT_DEFAULT, bg="#52c41a", fg="white",
                 command=self._ai_market_analysis).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_row, text="🔍 数据查询", font=self.FONT_DEFAULT, bg="#faad14", fg="black",
                 command=self._ai_data_query).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_row, text="💡 优化建议", font=self.FONT_DEFAULT, bg="#722ed1", fg="white",
                 command=self._ai_optimization_suggestions).pack(side=tk.LEFT, padx=5)

        # 对话区
        frm_chat = tk.LabelFrame(tab, text=" AI对话 ", font=self.FONT_BOLD, fg="#d46b08", padx=10, pady=10)
        frm_chat.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 聊天历史显示
        chat_frame = tk.Frame(frm_chat)
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        sb = ttk.Scrollbar(chat_frame, orient="vertical")
        self.ai_chat_text = tk.Text(chat_frame, bg="#f5f5f5", fg="#333",
                                    font=("PingFang SC", 12), yscrollcommand=sb.set,
                                    padx=10, pady=10, wrap=tk.WORD)
        sb.config(command=self.ai_chat_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.ai_chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 输入区
        input_frame = tk.Frame(frm_chat)
        input_frame.pack(fill=tk.X, pady=5)

        tk.Label(input_frame, text="提问:", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=5)
        self.ai_input = tk.Entry(input_frame, font=self.FONT_DEFAULT)
        self.ai_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.ai_input.bind("<Return>", lambda e: self._ai_send_message())

        ttk.Button(input_frame, text="发送", command=self._ai_send_message).pack(side=tk.LEFT, padx=5)
        ttk.Button(input_frame, text="清空", command=self._ai_clear_chat).pack(side=tk.LEFT, padx=5)

        # 加载配置
        self._load_ai_config()

    def _save_ai_config(self):
        """保存AI配置"""
        config = {"api_key": self.ai_api_key.get()}
        try:
            with open("ai_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("成功", "AI配置已保存")
        except Exception as e:
            messagebox.showerror("错误", f"保存配置失败: {e}")

    def _load_ai_config(self):
        """加载AI配置"""
        try:
            if os.path.exists("ai_config.json"):
                with open("ai_config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self.ai_api_key.delete(0, tk.END)
                    self.ai_api_key.insert(0, config.get("api_key", ""))
        except Exception as e:
            logger.warning(f"加载AI配置失败: {e}")

    def _test_ai_connection(self):
        """测试AI连接"""
        api_key = self.ai_api_key.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请先输入API Key")
            return

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=100,
                messages=[{"role": "user", "content": "Hello"}]
            )
            messagebox.showinfo("成功", "API连接测试成功！")
        except ImportError:
            messagebox.showerror("错误", "请先安装anthropic库：pip install anthropic")
        except Exception as e:
            messagebox.showerror("错误", f"连接失败: {e}")

    def _ai_append_message(self, role: str, content: str):
        """添加消息到聊天窗口"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = "🤖 AI" if role == "assistant" else "👤 你"
        self.ai_chat_text.insert(tk.END, f"\n[{timestamp}] {prefix}:\n{content}\n")
        self.ai_chat_text.see(tk.END)

    def _ai_send_message(self):
        """发送消息给AI"""
        message = self.ai_input.get().strip()
        if not message:
            return

        api_key = self.ai_api_key.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请先配置API Key")
            return

        self.ai_input.delete(0, tk.END)
        self._ai_append_message("user", message)

        def call_api():
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                context = self._build_ai_context()
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2000,
                    messages=[{"role": "user", "content": f"{context}\n\n用户问题：{message}"}]
                )
                answer = response.content[0].text
                self.root.after(0, lambda ans=answer: self._ai_append_message("assistant", ans))
            except ImportError:
                self.root.after(0, lambda: messagebox.showerror("错误", "请先安装anthropic库：pip install anthropic"))
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self._ai_append_message("assistant", f"❌ 错误: {msg}"))

        threading.Thread(target=call_api, daemon=True).start()

    def _build_ai_context(self) -> str:
        """构建AI上下文：当前数据统计"""
        context_parts = ["# 当前系统数据概览\n"]
        if self._batch_items:
            total = len(self._batch_items)
            on_shelf = len([i for i in self._batch_items if str(i.status_code) == "0" or i.status_name in ("已上架", "在售")])
            not_on_shelf = len([i for i in self._batch_items if i.status_name == "未上架"])
            sold = len([i for i in self._batch_items if i.status_name == "已售"])
            context_parts.append(f"## 商品统计")
            context_parts.append(f"- 总商品数: {total}")
            context_parts.append(f"- 已上架: {on_shelf}")
            context_parts.append(f"- 未上架: {not_on_shelf}")
            context_parts.append(f"- 已售: {sold}")
            prices = [int(i.current_price) for i in self._batch_items if i.current_price and i.current_price > 0]
            if prices:
                context_parts.append(f"\n## 价格统计")
                context_parts.append(f"- 平均价格: ¥{sum(prices)/len(prices):.0f}")
                context_parts.append(f"- 最高价格: ¥{max(prices)}")
                context_parts.append(f"- 最低价格: ¥{min(prices)}")
        if self.engine:
            context_parts.append(f"\n## 历史数据")
            context_parts.append(f"- 已加载历史成交数据")
        return "\n".join(context_parts)

    def _ai_pricing_analysis(self):
        """AI智能定价分析"""
        if not self._batch_items:
            messagebox.showwarning("提示", "请先导入商品数据")
            return
        api_key = self.ai_api_key.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请先配置API Key")
            return
        self._ai_append_message("user", "请分析当前商品的定价策略，给出优化建议")
        def analyze():
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                items_data = []
                for item in self._batch_items[:50]:
                    if str(item.status_code) == "0" or item.status_name in ("已上架", "在售"):
                        items_data.append({"型号": item.model, "成色": item.condition, "容量": item.capacity,
                                          "颜色": item.color, "当前价": int(item.current_price) if item.current_price else 0})
                prompt = f"""你是一个二手手机定价专家。请分析以下商品数据，给出定价优化建议：

{json.dumps(items_data, ensure_ascii=False, indent=2)}

请从以下角度分析：
1. 价格分布是否合理
2. 是否有明显的定价过高或过低的商品
3. 不同型号、成色、容量的定价策略建议
4. 市场竞争力分析
5. 具体的调价建议（哪些商品应该涨价/降价，幅度多少）"""
                response = client.messages.create(model="claude-sonnet-4-6", max_tokens=3000,
                                                 messages=[{"role": "user", "content": prompt}])
                answer = response.content[0].text
                self.root.after(0, lambda ans=answer: self._ai_append_message("assistant", ans))
            except ImportError:
                self.root.after(0, lambda: messagebox.showerror("错误", "请先安装anthropic库：pip install anthropic"))
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self._ai_append_message("assistant", f"❌ 分析失败: {msg}"))
        threading.Thread(target=analyze, daemon=True).start()

    def _ai_market_analysis(self):
        """AI市场趋势分析"""
        if not self.engine or not self._data_store.latest_date():
            messagebox.showwarning("提示", "请先同步历史数据")
            return
        api_key = self.ai_api_key.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请先配置API Key")
            return
        self._ai_append_message("user", "请分析历史成交数据，给出市场趋势分析")
        def analyze():
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                df = self._data_store.get_all_data()
                if df is None or df.empty:
                    self.root.after(0, lambda: self._ai_append_message("assistant", "❌ 没有历史数据"))
                    return
                stats = {"总成交量": len(df), "平均成交价": f"¥{df['price'].mean():.0f}",
                        "最高成交价": f"¥{df['price'].max():.0f}", "最低成交价": f"¥{df['price'].min():.0f}",
                        "热门型号": df['model'].value_counts().head(10).to_dict()}
                prompt = f"""你是一个二手手机市场分析专家。请根据以下历史成交数据统计，分析市场趋势：

{json.dumps(stats, ensure_ascii=False, indent=2)}

请分析：
1. 市场整体趋势（价格走势、成交量变化）
2. 热门型号分析
3. 价格区间分布
4. 给出未来定价策略建议"""
                response = client.messages.create(model="claude-sonnet-4-6", max_tokens=2000,
                                                 messages=[{"role": "user", "content": prompt}])
                answer = response.content[0].text
                self.root.after(0, lambda ans=answer: self._ai_append_message("assistant", ans))
            except ImportError:
                self.root.after(0, lambda: messagebox.showerror("错误", "请先安装anthropic库：pip install anthropic"))
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self._ai_append_message("assistant", f"❌ 分析失败: {msg}"))
        threading.Thread(target=analyze, daemon=True).start()

    def _ai_data_query(self):
        """AI数据查询"""
        self._ai_append_message("assistant", "你可以问我关于商品数据的任何问题，例如：\n- 有多少台iPhone 13在售？\n- 平均价格是多少？\n- 哪些商品价格偏高？\n- 成色为99新的商品有哪些？")

    def _ai_optimization_suggestions(self):
        """AI优化建议"""
        if not self._batch_items:
            messagebox.showwarning("提示", "请先导入商品数据")
            return
        api_key = self.ai_api_key.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请先配置API Key")
            return
        self._ai_append_message("user", "请给出库存优化和运营建议")
        def analyze():
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                on_shelf = [i for i in self._batch_items if str(i.status_code) == "0" or i.status_name in ("已上架", "在售")]
                not_on_shelf = [i for i in self._batch_items if i.status_name == "未上架"]
                model_dist = {}
                for item in on_shelf:
                    model_dist[item.model] = model_dist.get(item.model, 0) + 1
                stats = {"在架商品数": len(on_shelf), "未上架商品数": len(not_on_shelf),
                        "型号分布": dict(sorted(model_dist.items(), key=lambda x: x[1], reverse=True)[:10])}
                prompt = f"""你是一个二手手机运营专家。请根据以下库存数据，给出优化建议：

{json.dumps(stats, ensure_ascii=False, indent=2)}

请从以下角度给出建议：
1. 库存结构优化（哪些型号库存过多/过少）
2. 上架策略（未上架商品如何处理）
3. 定价策略优化
4. 促销建议
5. 风险提示"""
                response = client.messages.create(model="claude-sonnet-4-6", max_tokens=2000,
                                                 messages=[{"role": "user", "content": prompt}])
                answer = response.content[0].text
                self.root.after(0, lambda ans=answer: self._ai_append_message("assistant", ans))
            except ImportError:
                self.root.after(0, lambda: messagebox.showerror("错误", "请先安装anthropic库：pip install anthropic"))
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self._ai_append_message("assistant", f"❌ 分析失败: {msg}"))
        threading.Thread(target=analyze, daemon=True).start()

    def _ai_clear_chat(self):
        """清空聊天记录"""
        self.ai_chat_text.delete("1.0", tk.END)



    # ── 在架价格分析标签页 ────────────────────────────────────────────────────
    def _build_tab_price_monitor(self, nb: ttk.Notebook):
        """在架价格监控与异常分析"""
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 📊 在架价格分析 ")

        # 顶部控制区
        frm_ctrl = tk.LabelFrame(tab, text=" 分析设置 ", font=self.FONT_BOLD, fg="#1976d2", padx=10, pady=10)
        frm_ctrl.pack(fill=tk.X, padx=10, pady=10)

        # 异常阈值设置
        frm_threshold = tk.Frame(frm_ctrl)
        frm_threshold.pack(fill=tk.X, pady=5)

        tk.Label(frm_threshold, text="价格偏离阈值:", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=5)
        self.ent_price_deviation = tk.Entry(frm_threshold, width=8, font=self.FONT_DEFAULT)
        self.ent_price_deviation.insert(0, "15")  # 默认15%
        self.ent_price_deviation.pack(side=tk.LEFT, padx=5)
        tk.Label(frm_threshold, text="%", font=self.FONT_DEFAULT).pack(side=tk.LEFT)

        tk.Label(frm_threshold, text="滞销天数:", font=self.FONT_DEFAULT).pack(side=tk.LEFT, padx=(20, 5))
        self.ent_slow_days = tk.Entry(frm_threshold, width=8, font=self.FONT_DEFAULT)
        self.ent_slow_days.insert(0, "7")  # 默认7天
        self.ent_slow_days.pack(side=tk.LEFT, padx=5)
        tk.Label(frm_threshold, text="天", font=self.FONT_DEFAULT).pack(side=tk.LEFT)

        # 操作按钮
        frm_btn = tk.Frame(frm_ctrl)
        frm_btn.pack(fill=tk.X, pady=10)

        tk.Button(frm_btn, text="🔍 开始分析", font=self.FONT_BOLD, bg="#52c41a", fg="white",
                  command=self._start_price_analysis).pack(side=tk.LEFT, padx=5)
        tk.Button(frm_btn, text="📥 导出异常", font=self.FONT_DEFAULT, bg="#1890ff", fg="white",
                  command=self._export_anomalies).pack(side=tk.LEFT, padx=5)
        tk.Button(frm_btn, text="🔄 刷新", font=self.FONT_DEFAULT,
                  command=self._refresh_price_monitor).pack(side=tk.LEFT, padx=5)

        # 统计信息
        frm_stats = tk.LabelFrame(tab, text=" 统计概览 ", font=self.FONT_BOLD, fg="#fa8c16", padx=10, pady=10)
        frm_stats.pack(fill=tk.X, padx=10, pady=10)

        stats_grid = tk.Frame(frm_stats)
        stats_grid.pack(fill=tk.X)

        self.lbl_total_products = tk.Label(stats_grid, text="总商品数: 0", font=self.FONT_DEFAULT, fg="#333")
        self.lbl_total_products.grid(row=0, column=0, padx=15, pady=5, sticky=tk.W)

        self.lbl_high_price = tk.Label(stats_grid, text="价格过高: 0", font=self.FONT_DEFAULT, fg="#ff4d4f")
        self.lbl_high_price.grid(row=0, column=1, padx=15, pady=5, sticky=tk.W)

        self.lbl_low_price = tk.Label(stats_grid, text="价格过低: 0", font=self.FONT_DEFAULT, fg="#faad14")
        self.lbl_low_price.grid(row=0, column=2, padx=15, pady=5, sticky=tk.W)

        self.lbl_slow_sale = tk.Label(stats_grid, text="滞销商品: 0", font=self.FONT_DEFAULT, fg="#722ed1")
        self.lbl_slow_sale.grid(row=0, column=3, padx=15, pady=5, sticky=tk.W)

        # 异常商品列表
        frm_list = tk.LabelFrame(tab, text=" 异常商品列表 ", font=self.FONT_BOLD, fg="#d32f2f", padx=10, pady=10)
        frm_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 创建表格
        columns = ("状态", "串码/质检码", "商品名称", "当前价格", "建议价格", "成本底价", "行情参考")
        self.tree_anomalies = ttk.Treeview(frm_list, columns=columns, show="headings", height=15)

        self.tree_anomalies.heading("状态",     text="状态")
        self.tree_anomalies.heading("串码/质检码", text="串码/质检码")
        self.tree_anomalies.heading("商品名称",  text="商品名称")
        self.tree_anomalies.heading("当前价格",  text="当前价格")
        self.tree_anomalies.heading("建议价格",  text="建议价格")
        self.tree_anomalies.heading("成本底价",  text="成本底价")
        self.tree_anomalies.heading("行情参考",  text="行情参考")

        self.tree_anomalies.column("状态",      width=100, anchor="center")
        self.tree_anomalies.column("串码/质检码", width=140, anchor="center")
        self.tree_anomalies.column("商品名称",   width=260)
        self.tree_anomalies.column("当前价格",   width=80,  anchor="center")
        self.tree_anomalies.column("建议价格",   width=80,  anchor="center")
        self.tree_anomalies.column("成本底价",   width=80,  anchor="center")
        self.tree_anomalies.column("行情参考",   width=80,  anchor="center")

        # 滚动条
        vsb = ttk.Scrollbar(frm_list, orient="vertical", command=self.tree_anomalies.yview)
        self.tree_anomalies.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_anomalies.pack(fill=tk.BOTH, expand=True)

        # 双击查看详情
        self.tree_anomalies.bind("<Double-1>", self._show_anomaly_detail)

        # 右键菜单
        self.anomaly_menu = tk.Menu(self.tree_anomalies, tearoff=0)
        self.anomaly_menu.add_command(label="📝 快速调价", command=self._quick_reprice_anomaly)
        self.anomaly_menu.add_command(label="📊 查看历史", command=self._view_price_history)
        self.anomaly_menu.add_separator()
        self.anomaly_menu.add_command(label="❌ 标记忽略", command=self._ignore_anomaly)

        self.tree_anomalies.bind("<Button-2>", self._show_anomaly_menu)  # macOS右键
        self.tree_anomalies.bind("<Button-3>", self._show_anomaly_menu)  # Windows/Linux右键

        # 日志区
        frm_log = tk.Frame(tab)
        frm_log.pack(fill=tk.X, padx=14, pady=(0, 6))
        sb_log = ttk.Scrollbar(frm_log, orient="vertical")
        self.txt_monitor_log = tk.Text(frm_log, height=5, bg="#1e1e1e", fg="#a9b7c6",
                                       font=("Menlo", 11), yscrollcommand=sb_log.set, padx=6, pady=4)
        sb_log.config(command=self.txt_monitor_log.yview)
        sb_log.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_monitor_log.pack(fill=tk.X)

        # 初始化数据
        self._anomaly_data = []

    def _start_price_analysis(self):
        """开始价格分析"""
        self._monitor_log("🔍 开始分析在架商品价格...")

        try:
            deviation_threshold = float(self.ent_price_deviation.get()) / 100
            slow_days = int(self.ent_slow_days.get())
        except ValueError:
            messagebox.showerror("参数错误", "请输入有效的数字")
            return

        # 清空现有数据
        for item in self.tree_anomalies.get_children():
            self.tree_anomalies.delete(item)
        self._anomaly_data.clear()

        # 在新线程中执行分析
        threading.Thread(target=self._analyze_prices_thread,
                        args=(deviation_threshold, slow_days),
                        daemon=True).start()

    def _analyze_prices_thread(self, deviation_threshold, slow_days):
        """价格分析线程 —— 全量展示，多维度比对"""
        items = [it for it in self._batch_items if it.current_price > 0]
        total_count = len(items)
        high_count = low_count = floor_count = 0

        if not items:
            self.root.after(0, lambda: self._monitor_log(
                "⚠️ 批量调价列表为空，请先在「批量调价」页导入商品"))
            self.root.after(0, lambda: self._update_stats(0, 0, 0, 0))
            return

        self.root.after(0, lambda n=total_count: self._monitor_log(
            f"📦 共读取 {n} 件在架商品，开始全量比对..."))

        for item in items:
            cur        = int(item.current_price)
            suggested  = item.suggested_price if item.suggested_price else 0
            cost_floor = item.cost_floor

            # ── 行情参考：历史成交均价 ────────────────────────────────────
            market_ref = 0
            if self.engine and self.engine.df is not None:
                df = self.engine.df
                kw = item.model[:8] if item.model else item.title[:8]
                sub = df[df["机型"].str.contains(kw, case=False, na=False)] if kw else df.iloc[0:0]
                if len(sub) >= 3:
                    market_ref = int(sub["售价"].mean())

            # ── 异常判断 ──────────────────────────────────────────────────
            anomaly_types = []

            # 建议价优先，无建议价时用历史均价兜底
            ref = suggested or market_ref
            if ref > 0:
                ratio = (cur - ref) / ref
                if ratio > deviation_threshold:
                    anomaly_types.append("偏高")
                    high_count += 1
                elif ratio < -deviation_threshold:
                    anomaly_types.append("偏低")
                    low_count += 1

            # 低于ERP成本底价（有数据才判断）
            if cost_floor > 0 and cur < cost_floor:
                anomaly_types.append("低于底价")
                floor_count += 1

            status = " + ".join(anomaly_types) if anomaly_types else "正常"

            row = {
                "status":          status,
                "code":            item.imei or item.qc_code or "-",
                "title":           item.title,
                "current_price":   cur,
                "suggested_price": suggested,
                "cost_floor":      cost_floor,
                "market_ref":      market_ref,
                "is_anomaly":      bool(anomaly_types),
                "item_ref":        item,
            }
            self._anomaly_data.append(row)
            self.root.after(0, lambda d=row: self._add_anomaly_to_tree(d))

        self.root.after(0, lambda: self._update_stats(
            total_count, high_count, low_count, floor_count))
        anomaly_count = sum(1 for r in self._anomaly_data if r["is_anomaly"])
        self.root.after(0, lambda: self._monitor_log(
            f"✅ 分析完成！{total_count} 件商品，其中 {anomaly_count} 件异常"))

        # 更新统计信息
        self.root.after(0, lambda: self._update_stats(
            total_count, high_price_count, low_price_count, slow_sale_count))

        self.root.after(0, lambda: self._monitor_log(f"✅ 分析完成！共{total_count}件商品，发现{len(self._anomaly_data)}个异常"))

    def _add_anomaly_to_tree(self, data):
        """添加商品到表格（全量，正常绿色，异常彩色）"""
        t = data["status"]
        if "偏高" in t:
            tag = "high_price"
        elif "偏低" in t:
            tag = "low_price"
        elif "底价" in t:
            tag = "below_floor"
        elif t == "正常":
            tag = "normal"
        else:
            tag = "low_conf"

        cost_str   = f"¥{data['cost_floor']}"       if data["cost_floor"]       else "-"
        market_str = f"¥{data['market_ref']}"        if data["market_ref"]       else "-"
        sug_str    = f"¥{data['suggested_price']}"   if data["suggested_price"]  else "-"
        title      = data["title"]
        title_disp = title[:30] + "…" if len(title) > 30 else title

        self.tree_anomalies.insert("", tk.END, values=(
            t, data["code"], title_disp,
            f"¥{data['current_price']}", sug_str, cost_str, market_str,
        ), tags=(tag,))

        self.tree_anomalies.tag_configure("high_price",  foreground="#ff4d4f")
        self.tree_anomalies.tag_configure("low_price",   foreground="#faad14")
        self.tree_anomalies.tag_configure("below_floor", foreground="#722ed1")
        self.tree_anomalies.tag_configure("normal",      foreground="#52c41a")
        self.tree_anomalies.tag_configure("low_conf",    foreground="#8c8c8c")

    def _update_stats(self, total, high, low, floor):
        """更新统计信息"""
        self.lbl_total_products.config(text=f"总商品数: {total}")
        self.lbl_high_price.config(text=f"价格偏高: {high}")
        self.lbl_low_price.config(text=f"价格偏低: {low}")
        self.lbl_slow_sale.config(text=f"低于底价: {floor}")

    def _calculate_days_listed(self, create_time_ms):
        """计算上架天数"""
        if not create_time_ms:
            return 0
        create_time = datetime.fromtimestamp(create_time_ms / 1000)
        return (datetime.now() - create_time).days

    def _find_similar_sales(self, model_name):
        """查找相似商品的历史成交数据"""
        if self.engine is None or self.engine.df is None:
            return pd.DataFrame()

        # 简单的模糊匹配
        df = self.engine.df
        mask = df["机型"].str.contains(model_name[:10], case=False, na=False)
        return df[mask]

    def _show_anomaly_detail(self, event):
        """双击显示异常详情"""
        selection = self.tree_anomalies.selection()
        if not selection:
            return

        item = self.tree_anomalies.item(selection[0])
        values = item["values"]

        detail_win = tk.Toplevel(self.root)
        detail_win.title("异常详情")
        detail_win.geometry("500x400")

        tk.Label(detail_win, text="商品异常详情", font=self.FONT_BOLD, fg="#1976d2").pack(pady=10)

        info_frame = tk.Frame(detail_win)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        details = [
            ("状态:",      values[0]),
            ("串码/质检码:", values[1]),
            ("商品名称:",  values[2]),
            ("当前价格:",  values[3]),
            ("建议价格:",  values[4]),
            ("成本底价:",  values[5]),
            ("行情参考:",  values[6]),
        ]

        for i, (label, value) in enumerate(details):
            tk.Label(info_frame, text=label, font=self.FONT_DEFAULT, anchor=tk.W).grid(
                row=i, column=0, sticky=tk.W, pady=5, padx=5)
            tk.Label(info_frame, text=value, font=self.FONT_DEFAULT, fg="#333", anchor=tk.W).grid(
                row=i, column=1, sticky=tk.W, pady=5, padx=5)

        btn_frame = tk.Frame(detail_win)
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="立即调价", font=self.FONT_DEFAULT, bg="#52c41a", fg="white",
                  command=lambda: [self._quick_reprice_anomaly(), detail_win.destroy()]).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="关闭", font=self.FONT_DEFAULT,
                  command=detail_win.destroy).pack(side=tk.LEFT, padx=5)

    def _show_anomaly_menu(self, event):
        """显示右键菜单"""
        item = self.tree_anomalies.identify_row(event.y)
        if item:
            self.tree_anomalies.selection_set(item)
            self.anomaly_menu.post(event.x_root, event.y_root)

    def _quick_reprice_anomaly(self):
        """快速调价"""
        selection = self.tree_anomalies.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择要调价的商品")
            return

        item = self.tree_anomalies.item(selection[0])
        values = item["values"]

        # 提取建议价格
        suggested_price_str = values[3]
        if suggested_price_str == "-":
            messagebox.showinfo("提示", "该商品没有建议价格")
            return

        suggested_price = int(suggested_price_str.replace("¥", ""))

        result = messagebox.askyesno(
            "确认调价",
            f"商品: {values[1]}\n"
            f"当前价格: {values[2]}\n"
            f"建议价格: {values[3]}\n\n"
            f"确认调整为建议价格？"
        )

        if result:
            self._monitor_log(f"🔄 正在调价: {values[1]}")
            # TODO: 实现实际的调价逻辑
            messagebox.showinfo("成功", "调价功能开发中...")

    def _view_price_history(self):
        """查看价格历史"""
        messagebox.showinfo("提示", "价格历史功能开发中...")

    def _ignore_anomaly(self):
        """标记忽略异常"""
        selection = self.tree_anomalies.selection()
        if selection:
            self.tree_anomalies.delete(selection[0])
            self._monitor_log("✓ 已标记忽略")

    def _export_anomalies(self):
        """导出异常商品列表"""
        if not self._anomaly_data:
            messagebox.showwarning("提示", "没有异常数据可导出")
            return

        try:
            df = pd.DataFrame(self._anomaly_data)
            filename = f"price_anomalies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df.to_csv(filename, index=False, encoding="utf-8-sig")
            self._monitor_log(f"✅ 已导出到: {filename}")
            messagebox.showinfo("成功", f"异常数据已导出到:\n{filename}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _refresh_price_monitor(self):
        """刷新价格监控"""
        self._start_price_analysis()

    def _monitor_log(self, msg: str):
        """向在架分析日志写入一条消息"""
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_monitor_log.insert(tk.END, f"[{ts}] {msg}\n")
        self.txt_monitor_log.see(tk.END)

# ─── 入口 ────────────────────────────────────────────────────────────────────
class ErpSoldImporter:
    """
    把爱管机 ERP 的成交记录解析成 SoldRecord，
    与现有 zhuanzhuan_sold_cache.csv 合并入库。
    独立运行，不改动 DataFetcher / PricingEngine。
    """

    # ERP selected_attr 字段映射
    _CONDITION_MAP = {
        "95新": "95新", "9成新": "9成新（轻微使用）",
        "8成新": "8成新（明显使用）", "7成新": "7成新",
        "A级": "95新", "B级": "9成新（轻微使用）",
        "C级": "8成新（明显使用）",
    }

    def __init__(self, token: str, version: str = ""):
        self.fetcher = ErpFetcher(token, version)
        self.store   = DataStore()

    def run(self, on_log=None) -> tuple[int, int]:
        """
        拉取 ERP 成交记录，解析后与本地缓存合并。
        返回 (新增条数, 跳过重复条数)
        """
        def log(m):
            if on_log: on_log(m)

        log("📥 从爱管机拉取成交记录...")
        try:
            raw_items = self.fetcher.fetch_sold(on_log=on_log)
        except Exception as e:
            raise RuntimeError(f"ERP成交记录拉取失败：{e}")

        log(f"  共拉取 {len(raw_items)} 条原始记录，开始解析...")
        records = []
        skipped_parse = 0
        for it in raw_items:
            r = self._parse(it)
            if r:
                records.append(r)
            else:
                skipped_parse += 1

        log(f"  解析成功 {len(records)} 条，跳过无效 {skipped_parse} 条")
        if not records:
            return 0, 0

        # 合并到本地缓存
        existing_df = self.store.load()
        new_df      = DataFetcher.build_dataframe(records)  # 复用现有 build_dataframe

        if existing_df.empty:
            merged = new_df
        else:
            merged = DataStore.merge(existing_df, new_df)

        added   = len(merged) - len(existing_df)
        skipped = len(new_df) - max(added, 0)
        self.store.save(merged)
        log(f"✅ ERP成交合并完成：新增 {added} 条，重复跳过 {skipped} 条，缓存共 {len(merged)} 条")
        return added, skipped

    def run_from_cache(self, on_log=None) -> tuple[int, int]:
        """用已缓存的原始数据直接导入，不重复请求 ERP"""
        raw_items = getattr(self, "_cached_raw", [])
        if not raw_items:
            raise RuntimeError("无缓存数据，请先点「拉取并预览」")
        def log(m):
            if on_log: on_log(m)
        records = [r for r in (self._parse(it) for it in raw_items) if r]
        log(f"  解析 {len(records)} 条，开始合并...")
        existing_df = self.store.load()
        new_df      = DataFetcher.build_dataframe(records)
        if existing_df.empty:
            merged = new_df
        else:
            merged = DataStore.merge(existing_df, new_df)
        added   = len(merged) - len(existing_df)
        skipped = len(new_df) - max(added, 0)
        self.store.save(merged)
        log(f"✅ 导入完成：新增 {added} 条，重复 {skipped} 条，缓存共 {len(merged)} 条")
        return added, skipped

    def _parse(self, it: dict) -> Optional[SoldRecord]:
        """把 ERP 一条成交记录解析成 SoldRecord"""
        # 只处理已成功成交的记录（status 通常为已售出状态）
        sale_amount = it.get("sale_amount")
        if not sale_amount:
            return None

        name  = it.get("name", "")
        attrs = it.get("selected_attr") or {}
        memory = attrs.get("内存") or it.get("product_attr_memory") or ""
        color  = attrs.get("颜色") or it.get("product_attr_color") or ""

        # 成色：ERP 用 A/B/C 级，映射到转转成色描述
        raw_cond = attrs.get("成色") or attrs.get("外观成色") or ""
        condition = self._CONDITION_MAP.get(raw_cond, raw_cond or "9成新（轻微使用）")

        # 型号：去掉品牌前缀
        model = _BRAND_PREFIX_RE.sub("", name).strip()

        # 时间
        def _parse_dt(s):
            if not s: return None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try: return datetime.strptime(s, fmt)
                except: pass
            return None

        list_time = _parse_dt(it.get("put_shelf_time") or it.get("purchase_time"))
        sold_time = _parse_dt(it.get("success_time") or it.get("update_time"))

        try:
            sell_price = float(sale_amount)   # 转转成交价，直接用，不加减
        except Exception:
            return None

        # 到手价对齐逻辑：
        #   ERP settle_amount = 转转到手价 - 40元站点服务费
        #   → 转转到手价 = settle_amount + 40（这是我们存入定价引擎的 settle_price）
        try:
            settle_raw = float(it.get("settle_amount") or 0)
            # 有真实结算数据：还原转转口径的到手价
            settle = (settle_raw + STATION_SERVICE_FEE) if settle_raw > 0 else round(sell_price * (1 - PLATFORM_FEE_RATE), 2)
        except Exception:
            settle = round(sell_price * (1 - PLATFORM_FEE_RATE), 2)

        if not model or sell_price <= 0:
            return None

        return SoldRecord(
            model        = model,
            condition    = condition,
            capacity     = memory,
            color        = color,
            list_time    = list_time,
            sold_time    = sold_time,
            sell_price   = sell_price,   # 转转成交价（与转转历史数据口径一致）
            settle_price = settle,        # 转转口径到手价（= ERP settle + 40）
        )


class MarketScanner:
    """
    扫描转转市场上同款在售机器的价格分布。
    通过转转商品列表接口，搜索关键词，返回价格分位数。
    独立运行，不修改任何现有数据。
    """

    SEARCH_URL = "https://b.zhuanzhuan.com/gatewayapi/scm_render_product/merchantProductList"

    def __init__(self, cookie: str):
        self.cookie = cookie
        self.session = requests.Session()
        self.session.headers.update({
            "Cookie":     cookie,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        })

    def scan(self, model: str, condition: str, capacity: str,
             on_log=None) -> dict:
        """
        搜索转转上同款机器的在售价格，返回分析结果。
        返回格式：{
            "count": int,
            "p25": float, "p50": float, "p75": float,
            "min": float, "max": float,
            "prices": list[float],   # 原始价格列表
        }
        """
        def log(m):
            if on_log: on_log(m)

        keyword = f"{model} {capacity}".strip()
        payload = {
            "page": 1, "pageSize": 50,
            "keyword": keyword,
            "sortType": 0,
        }

        log(f"  🔍 搜索市场：{keyword} {condition}...")
        prices = []
        raw_items_detail = []   # 供明细表显示
        for page in range(1, 4):   # 最多扫3页，约150条
            payload["page"] = page
            try:
                resp = self.session.post(self.SEARCH_URL, json=payload, timeout=10)
                body = resp.json()
            except Exception as e:
                log(f"  ⚠️ 第{page}页失败：{e}")
                break

            items = []
            data = body.get("data", {})
            if isinstance(data, dict):
                items = data.get("list") or data.get("data") or []
            elif isinstance(data, list):
                items = data

            if not items:
                break

            for it in items:
                # 过滤成色不匹配的（关键词搜会混入其他成色）
                item_cond = (it.get("goodsGrade") or it.get("condition") or "")
                if condition and item_cond and condition[:3] not in item_cond:
                    continue
                price_info = it.get("priceInfo") or it.get("price") or {}
                p = 0
                if isinstance(price_info, dict):
                    p = (price_info.get("sellingPrice") or price_info.get("price") or 0)
                    if p > 100:   # 单位分 → 元
                        p = p / 100
                elif isinstance(price_info, (int, float)):
                    p = float(price_info)
                if p > 100:
                    prices.append(p)
                    raw_items_detail.append({
                        "price": p,
                        "cond":  item_cond or "—",
                        "title": it.get("title") or it.get("name") or "—",
                    })

        if not prices:
            return {"count": 0, "p25": 0, "p50": 0, "p75": 0, "min": 0, "max": 0, "prices": [], "_items": []}

        prices.sort()
        n = len(prices)

        def percentile(lst, pct):
            idx = int(len(lst) * pct / 100)
            return lst[min(idx, len(lst)-1)]

        result = {
            "count": n,
            "p25":   percentile(prices, 25),
            "p50":   percentile(prices, 50),
            "p75":   percentile(prices, 75),
            "min":   prices[0],
            "max":   prices[-1],
            "prices": prices,
            "_items": raw_items_detail,   # 供明细表显示
        }
        log(f"  📊 找到 {n} 条在售：P25=¥{result['p25']:.0f}  中位=¥{result['p50']:.0f}  P75=¥{result['p75']:.0f}")
        return result


class SoldAnalyzer:
    """
    对本地缓存的成交记录做动销时长分层分析。
    基于现有 zhuanzhuan_sold_cache.csv，不修改数据，只做读取分析。
    """

    FAST_HOURS = 24    # 极速动销阈值（小时）

    def __init__(self):
        self.store = DataStore()

    def analyze(self, model: str, condition: str, capacity: str,
                color: str = "", days: int = 30) -> dict:
        """
        分析指定型号的动销时长分布，返回分层定价建议。
        返回格式：{
            "total":       int,        # 样本总数
            "fast_count":  int,        # 24h内成交数
            "fast_p40":    float,      # 极速动销价（P40）
            "all_p50":     float,      # 中位参考价
            "floor_p20":   float,      # 底价预警（P20）
            "avg_hours":   float,      # 平均动销时长（小时）
            "fast_ratio":  float,      # 极速动销率（%）
            "recommendation": str,     # 文字建议
        }
        """
        df = self.store.load()
        if df.empty:
            return self._empty()

        since = datetime.now() - pd.Timedelta(days=days)

        # 筛选条件
        mask = df["型号"].str.contains(model[:8], na=False, case=False)
        if condition:
            mask &= df["精确成色"].str.contains(condition[:4], na=False)
        if capacity:
            mask &= df["容量"].str.contains(capacity.replace("G","").replace("g",""), na=False)
        if color:
            mask &= df["颜色"].str.contains(color[:2], na=False)

        # 时间过滤
        if "售出时间" in df.columns:
            df["售出时间"] = pd.to_datetime(df["售出时间"], errors="coerce")
            mask &= df["售出时间"] >= since

        sub = df[mask].copy()
        if sub.empty:
            return self._empty()

        # 计算动销时长
        if "上架时间" in sub.columns and "售出时间" in sub.columns:
            sub["上架时间"] = pd.to_datetime(sub["上架时间"], errors="coerce")
            sub["动销时长_h"] = (sub["售出时间"] - sub["上架时间"]).dt.total_seconds() / 3600
            sub = sub[sub["动销时长_h"] > 0]   # 过滤异常
        else:
            sub["动销时长_h"] = float("nan")

        prices = sub["最终售价"].dropna().tolist()
        prices.sort()
        if not prices:
            return self._empty()

        def pct(lst, p):
            if not lst: return 0
            idx = max(0, int(len(lst) * p / 100) - 1)
            return lst[idx]

        fast_mask = sub["动销时长_h"] <= self.FAST_HOURS
        fast_prices = sub.loc[fast_mask, "最终售价"].dropna().sort_values().tolist()
        avg_hours   = sub["动销时长_h"].mean() if not sub["动销时长_h"].isna().all() else 0
        fast_ratio  = len(fast_prices) / len(prices) * 100 if prices else 0

        fast_p40 = pct(fast_prices, 40) if fast_prices else pct(prices, 40)
        all_p50  = pct(prices, 50)
        floor    = pct(prices, 20)

        # 文字建议
        if avg_hours < 12:
            rec = f"该款动销极快（均{avg_hours:.0f}h），当前定价具有竞争力，可适当上调¥50-100试探"
        elif avg_hours < 48:
            rec = f"动销正常（均{avg_hours:.0f}h），维持极速动销价¥{fast_p40:.0f}即可"
        else:
            rec = f"动销偏慢（均{avg_hours:.0f}h），建议降至保守价¥{all_p50:.0f}，若超7天考虑底价¥{floor:.0f}"

        return {
            "total":          len(prices),
            "fast_count":     len(fast_prices),
            "fast_p40":       round(fast_p40),
            "all_p50":        round(all_p50),
            "floor_p20":      round(floor),
            "avg_hours":      round(avg_hours, 1),
            "fast_ratio":     round(fast_ratio, 1),
            "recommendation": rec,
        }

    def _empty(self) -> dict:
        return {"total": 0, "fast_count": 0, "fast_p40": 0, "all_p50": 0,
                "floor_p20": 0, "avg_hours": 0, "fast_ratio": 0,
                "recommendation": "暂无历史数据"}

    def analyze_with_rows(self, model: str, condition: str, capacity: str,
                          color: str = "", days: int = 30) -> tuple[dict, list]:
        """同 analyze，但额外返回每条原始记录供明细表显示"""
        result = self.analyze(model, condition, capacity, color, days)
        # 重新读一次 sub，构造明细行
        df = self.store.load()
        rows = []
        if df.empty:
            return result, rows
        since = datetime.now() - pd.Timedelta(days=days)
        mask = df["型号"].str.contains(model[:8], na=False, case=False)
        if condition: mask &= df["精确成色"].str.contains(condition[:4], na=False)
        if capacity:  mask &= df["容量"].str.contains(capacity.replace("G","").replace("g",""), na=False)
        if color:     mask &= df["颜色"].str.contains(color[:2], na=False)
        if "售出时间" in df.columns:
            df["售出时间"] = pd.to_datetime(df["售出时间"], errors="coerce")
            mask &= df["售出时间"] >= since
        sub = df[mask].copy()
        if sub.empty:
            return result, rows
        if "上架时间" in sub.columns and "售出时间" in sub.columns:
            sub["上架时间"] = pd.to_datetime(sub["上架时间"], errors="coerce")
            sub["动销时长_h"] = (sub["售出时间"] - sub["上架时间"]).dt.total_seconds() / 3600
        else:
            sub["动销时长_h"] = float("nan")
        for _, row in sub.iterrows():
            h = row.get("动销时长_h", float("nan"))
            rows.append({
                "model":     str(row.get("型号","")),
                "condition": str(row.get("精确成色","")),
                "capacity":  str(row.get("容量","")),
                "color":     str(row.get("颜色","")),
                "price":     float(row.get("最终售价") or 0),
                "hours":     float(h) if not pd.isna(h) else -1,
                "fast":      (not pd.isna(h)) and h <= self.FAST_HOURS,
                "list_time": str(row.get("上架时间",""))[:16],
                "sold_time": str(row.get("售出时间",""))[:16],
            })
        return result, rows


class DataLabWindow:
    """
    数据实验室独立浮窗。
    包含三个子标签：ERP成交导入 / 市场行情扫描 / 动销分析。
    """

    COND_LIST = [
        "95新", "9成新（轻微使用）", "8成新（明显使用）",
        "7成新", "6成新", "5成新及以下",
    ]
    CAP_LIST  = ["64G","128G","256G","512G","1T","12GB+256G","12GB+512G","16GB+512G","16GB+1T"]

    def __init__(self, parent_app):
        self.app  = parent_app
        self.root = parent_app.root
        self._win: Optional[tk.Toplevel] = None

    def open(self):
        if self._win and self._win.winfo_exists():
            self._win.lift(); self._win.focus_force(); return

        win = tk.Toplevel(self.root)
        win.title("🔬 数据实验室（独立测试模块）")
        win.geometry("960x700")
        win.minsize(800, 560)
        self._win = win

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._build_erp_tab(nb)
        self._build_market_tab(nb)
        self._build_analyze_tab(nb)

    def _get_cache_options(self) -> tuple[list, list, list]:
        """从本地缓存读取型号/成色/容量选项"""
        try:
            df = DataStore().load()
            if df.empty:
                return [], self.COND_LIST, self.CAP_LIST
            models = sorted(df["型号"].dropna().unique().tolist())
            conds  = sorted(df["精确成色"].dropna().unique().tolist()) or self.COND_LIST
            caps   = sorted(df["容量"].dropna().unique().tolist()) or self.CAP_LIST
            return models, conds, caps
        except Exception:
            return [], self.COND_LIST, self.CAP_LIST

    # ── Tab1: ERP成交导入 ─────────────────────────────────────────────────────
    def _build_erp_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 📥 ERP成交导入 ")

        frm = tk.LabelFrame(tab, text=" 爱管机配置（自动读取自动化页的 Token）",
                            font=("PingFang SC", 12, "bold"), padx=10, pady=8)
        frm.pack(fill=tk.X, padx=14, pady=(10, 4))

        r1 = tk.Frame(frm); r1.pack(fill=tk.X, pady=2)
        tk.Label(r1, text="Authorization:", font=("PingFang SC", 12)).pack(side=tk.LEFT)
        self._erp1_token = tk.StringVar(value=self.app.erp_config.token)
        e = ttk.Entry(r1, textvariable=self._erp1_token, width=36, show="*", font=("Menlo", 11))
        e.pack(side=tk.LEFT, padx=6)
        tk.Button(r1, text="👁", font=("PingFang SC",11), width=2,
                  command=lambda: e.config(show="" if e.cget("show")=="*" else "*")
                  ).pack(side=tk.LEFT)

        r2 = tk.Frame(frm); r2.pack(fill=tk.X, pady=2)
        tk.Label(r2, text="Version:          ", font=("PingFang SC", 12)).pack(side=tk.LEFT)
        self._erp1_ver = tk.StringVar(value=self.app.erp_config.version)
        ttk.Entry(r2, textvariable=self._erp1_ver, width=20, font=("Menlo", 11)).pack(side=tk.LEFT, padx=6)
        tk.Label(r2, text="← 从抓包 Headers 里复制",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=6)

        btn_row = tk.Frame(tab); btn_row.pack(fill=tk.X, padx=14, pady=6)
        tk.Button(btn_row, text="▶ 拉取并预览（不入库）", font=("PingFang SC", 12),
                  bg="#fa8c16", fg="black", padx=10,
                  command=self._run_erp_preview).pack(side=tk.LEFT, padx=(0,8))
        tk.Button(btn_row, text="✅ 确认导入到定价缓存", font=("PingFang SC", 12),
                  bg="#52c41a", fg="black", padx=10,
                  command=self._run_erp_import).pack(side=tk.LEFT, padx=(0,16))
        self._erp1_status = tk.Label(btn_row, text="", font=("PingFang SC", 11), fg="#888")
        self._erp1_status.pack(side=tk.LEFT)

        # ── 原始数据明细表 ────────────────────────────────────────────────────
        tree_frm = tk.LabelFrame(tab, text=" 原始数据预览（核对到手价是否正确）",
                                 font=("PingFang SC", 12, "bold"), padx=6, pady=6)
        tree_frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        cols = ("型号", "成色", "容量", "颜色", "转转成交价", "ERP到手价", "转转到手价(+40)", "上架时间", "成交时间")
        self._erp1_tree = ttk.Treeview(tree_frm, columns=cols, show="headings", height=10)
        widths = [150, 90, 60, 70, 80, 80, 100, 120, 120]
        for col, w in zip(cols, widths):
            self._erp1_tree.heading(col, text=col)
            self._erp1_tree.column(col, width=w, anchor="center")
        vsb = ttk.Scrollbar(tree_frm, orient="vertical", command=self._erp1_tree.yview)
        hsb = ttk.Scrollbar(tree_frm, orient="horizontal", command=self._erp1_tree.xview)
        self._erp1_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._erp1_tree.pack(fill=tk.BOTH, expand=True)

        self._erp1_log = self._make_log(tab, height=4)
        self._erp1_raw: list[dict] = []   # 缓存原始数据供确认导入用

    def _run_erp_preview(self):
        """只拉取数据展示，不写入缓存，让用户核对"""
        token = self._erp1_token.get().strip()
        ver   = self._erp1_ver.get().strip()
        if not token:
            messagebox.showwarning("提示", "请填写 Token", parent=self._win); return
        self._log_clear(self._erp1_log)
        for row in self._erp1_tree.get_children():
            self._erp1_tree.delete(row)
        self._erp1_raw.clear()
        self._erp1_status.config(text="拉取中...", fg="#1890ff")

        def _worker():
            try:
                fetcher = ErpFetcher(token, ver)
                items = fetcher.fetch_sold(on_log=lambda m: self.root.after(
                    0, lambda msg=m: self._log_append(self._erp1_log, msg)))
                self.root.after(0, lambda its=items: self._fill_erp_preview(its))
            except Exception as e:
                self.root.after(0, lambda: (
                    self._log_append(self._erp1_log, f"❌ 失败：{e}"),
                    self._erp1_status.config(text=f"❌ {e}", fg="#cf1322"),
                ))
        threading.Thread(target=_worker, daemon=True).start()

    def _fill_erp_preview(self, items: list[dict]):
        self._erp1_raw = items
        importer = ErpSoldImporter.__new__(ErpSoldImporter)
        importer.store = DataStore()
        count = valid = 0
        for it in items:
            count += 1
            r = importer._parse(it)
            if not r: continue
            valid += 1
            erp_sale    = float(it.get("sale_amount") or 0)          # 转转成交价
            erp_settle  = float(it.get("settle_amount") or 0)        # ERP到手价（已扣站点费）
            zz_settle   = (erp_settle + STATION_SERVICE_FEE) if erp_settle > 0 else 0
            settle_disp = f"¥{erp_settle:.0f}" if erp_settle > 0 else "未结算"
            zz_disp     = f"¥{zz_settle:.0f}" if zz_settle > 0 else "—"
            self._erp1_tree.insert("", tk.END, values=(
                r.model, r.condition, r.capacity, r.color,
                f"¥{erp_sale:.0f}",
                settle_disp,
                zz_disp,
                r.list_time.strftime("%m-%d %H:%M") if r.list_time else "—",
                r.sold_time.strftime("%m-%d %H:%M") if r.sold_time else "—",
            ))
        self._erp1_status.config(
            text=f"共 {count} 条，可解析 {valid} 条 — 核对到手价无误后点「确认导入」",
            fg="#389e0d" if valid else "#cf1322")
        self._log_append(self._erp1_log, f"✅ 预览完成：{count} 条原始记录，{valid} 条可导入")

    def _run_erp_import(self):
        if not self._erp1_raw:
            messagebox.showwarning("提示", "请先点「拉取并预览」", parent=self._win); return
        token = self._erp1_token.get().strip()
        ver   = self._erp1_ver.get().strip()
        self._erp1_status.config(text="导入中...", fg="#1890ff")
        def _worker():
            try:
                importer = ErpSoldImporter(token, ver)
                # 直接用已拉好的数据，不重复请求
                importer._cached_raw = self._erp1_raw
                added, skipped = importer.run_from_cache(
                    on_log=lambda m: self.root.after(
                        0, lambda msg=m: self._log_append(self._erp1_log, msg)))
                self.root.after(0, lambda: (
                    self._erp1_status.config(
                        text=f"✅ 导入完成：新增 {added} 条，重复 {skipped} 条", fg="#389e0d"),
                    self._reload_engine(),
                ))
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self._erp1_status.config(text=f"❌ {msg}", fg="#cf1322"))
        threading.Thread(target=_worker, daemon=True).start()

    def _reload_engine(self):
        try:
            df = DataStore().load()
            if not df.empty:
                if "sales_hours" not in df.columns:
                    df["sales_hours"] = (
                        (df["售出时间"] - df["上架时间"]).dt.total_seconds() / 3600
                    )
                df = df[df["sales_hours"] >= 0].reset_index(drop=True)
                self.app.engine = PricingEngine(df)  # FIX: 传 DataFrame 而非 list
                self._log_append(self._erp1_log, f"✅ 定价引擎已重载，共 {len(df)} 条记录")
        except Exception as e:
            self._log_append(self._erp1_log, f"⚠️ 引擎重载失败：{e}")

    # ── Tab2: 市场行情扫描 ────────────────────────────────────────────────────
    def _build_market_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" 📡 市场行情扫描 ")

        models, conds, caps = self._get_cache_options()

        frm = tk.LabelFrame(tab, text=" 搜索条件（下拉选项来自本地缓存）",
                            font=("PingFang SC", 12, "bold"), padx=10, pady=8)
        frm.pack(fill=tk.X, padx=14, pady=(10, 4))

        r1 = tk.Frame(frm); r1.pack(fill=tk.X, pady=4)
        tk.Label(r1, text="型号：", font=("PingFang SC", 12)).pack(side=tk.LEFT)
        self._mkt_model = tk.StringVar()
        cb_model = ttk.Combobox(r1, textvariable=self._mkt_model, values=models,
                                 width=22, font=("PingFang SC", 12))
        cb_model.pack(side=tk.LEFT, padx=6)

        tk.Label(r1, text="容量：", font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=(12,0))
        self._mkt_cap = tk.StringVar()
        ttk.Combobox(r1, textvariable=self._mkt_cap, values=caps,
                     width=9, font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=6)

        tk.Label(r1, text="成色：", font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=(12,0))
        self._mkt_cond = tk.StringVar()
        ttk.Combobox(r1, textvariable=self._mkt_cond, values=conds,
                     width=18, font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=6)

        r2 = tk.Frame(frm); r2.pack(fill=tk.X, pady=4)
        tk.Label(r2, text="使用店铺：", font=("PingFang SC", 12)).pack(side=tk.LEFT)
        self._mkt_account = ttk.Combobox(r2, state="readonly", width=18, font=("PingFang SC", 12))
        names = [a.name for a in self.app.account_mgr.accounts]
        self._mkt_account["values"] = names
        if names: self._mkt_account.set(names[0])
        self._mkt_account.pack(side=tk.LEFT, padx=6)
        tk.Label(r2, text="（用该店铺 Cookie 调用转转搜索接口）",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=6)

        btn_row = tk.Frame(tab); btn_row.pack(fill=tk.X, padx=14, pady=6)
        tk.Button(btn_row, text="🔍 扫描市场", font=("PingFang SC", 12),
                  bg="#1890ff", fg="black", padx=12,
                  command=self._run_market_scan).pack(side=tk.LEFT)

        # 汇总卡片
        card_frm = tk.LabelFrame(tab, text=" 价格分布汇总",
                                 font=("PingFang SC", 12, "bold"), padx=10, pady=6)
        card_frm.pack(fill=tk.X, padx=14, pady=(0, 4))
        self._mkt_labels = {}
        for key, label, color in [("count","在售数量","#333"),("p25","P25 低价位","#52c41a"),
                                   ("p50","中位价","#1890ff"),("p75","P75 高价位","#fa8c16"),
                                   ("min","最低","#ff4d4f"),("max","最高","#722ed1")]:
            col = tk.Frame(card_frm); col.pack(side=tk.LEFT, padx=18)
            tk.Label(col, text=label, font=("PingFang SC", 11), fg="#888").pack()
            lbl = tk.Label(col, text="—", font=("PingFang SC", 16, "bold"), fg=color)
            lbl.pack()
            self._mkt_labels[key] = lbl

        # 原始价格明细表
        tree_frm = tk.LabelFrame(tab, text=" 原始价格明细（所有抓到的在售商品）",
                                 font=("PingFang SC", 12, "bold"), padx=6, pady=6)
        tree_frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 4))
        cols = ("序号", "价格", "成色", "标题")
        self._mkt_tree = ttk.Treeview(tree_frm, columns=cols, show="headings", height=8)
        for col, w in zip(cols, [50, 80, 100, 400]):
            self._mkt_tree.heading(col, text=col)
            self._mkt_tree.column(col, width=w, anchor="w" if col=="标题" else "center")
        msb = ttk.Scrollbar(tree_frm, orient="vertical", command=self._mkt_tree.yview)
        self._mkt_tree.configure(yscrollcommand=msb.set)
        msb.pack(side=tk.RIGHT, fill=tk.Y)
        self._mkt_tree.pack(fill=tk.BOTH, expand=True)

        self._mkt_log = self._make_log(tab, height=3)

    def _run_market_scan(self):
        model = self._mkt_model.get().strip()
        cap   = self._mkt_cap.get().strip()
        cond  = self._mkt_cond.get().strip()
        if not model:
            messagebox.showwarning("提示", "请选择或输入型号", parent=self._win); return
        name   = self._mkt_account.get()
        cookie = next((a.cookie for a in self.app.account_mgr.accounts
                       if a.name == name), None)
        if not cookie:
            messagebox.showwarning("提示", "请选择有效店铺", parent=self._win); return
        self._log_clear(self._mkt_log)
        for row in self._mkt_tree.get_children(): self._mkt_tree.delete(row)
        for lbl in self._mkt_labels.values(): lbl.config(text="—")

        def _worker():
            try:
                scanner = MarketScanner(cookie)
                result  = scanner.scan(model, cond, cap,
                                       on_log=lambda m: self.root.after(
                                           0, lambda msg=m: self._log_append(self._mkt_log, msg)))
                self.root.after(0, lambda r=result: self._show_market_result(r))
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self._log_append(self._mkt_log, f"❌ 失败：{msg}"))
        threading.Thread(target=_worker, daemon=True).start()

    def _show_market_result(self, r: dict):
        fmt = {"count": f"{r['count']} 件", "p25": f"¥{r['p25']:.0f}",
               "p50": f"¥{r['p50']:.0f}", "p75": f"¥{r['p75']:.0f}",
               "min": f"¥{r['min']:.0f}", "max": f"¥{r['max']:.0f}"}
        for key, val in fmt.items():
            self._mkt_labels[key].config(text=val)
        # 明细表 — 用 raw items 填充（scanner 存在 _last_items）
        for i, item in enumerate(r.get("_items", []), 1):
            self._mkt_tree.insert("", tk.END, values=(
                i, f"¥{item['price']:.0f}", item.get("cond","—"), item.get("title","—")
            ))

    # ── Tab3: 动销时长分析 ────────────────────────────────────────────────────
    def _build_analyze_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text=" ⏱ 动销分析 ")

        models, conds, caps = self._get_cache_options()

        frm = tk.LabelFrame(tab, text=" 查询条件（下拉选项来自本地缓存）",
                            font=("PingFang SC", 12, "bold"), padx=10, pady=8)
        frm.pack(fill=tk.X, padx=14, pady=(10, 4))

        r1 = tk.Frame(frm); r1.pack(fill=tk.X, pady=4)
        for label, attr, vals, w in [
            ("型号：","_ana_model", models, 22),
            ("容量：","_ana_cap",   caps,   10),
            ("成色：","_ana_cond",  conds,  18),
        ]:
            tk.Label(r1, text=label, font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=(8,0))
            var = tk.StringVar()
            setattr(self, attr, var)
            ttk.Combobox(r1, textvariable=var, values=vals, width=w,
                         font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=4)

        tk.Label(r1, text="颜色：", font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=(8,0))
        self._ana_color = tk.StringVar()
        ttk.Entry(r1, textvariable=self._ana_color, width=10,
                  font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=4)

        r2 = tk.Frame(frm); r2.pack(fill=tk.X, pady=4)
        tk.Label(r2, text="参考天数：", font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=(8,0))
        self._ana_days = tk.StringVar(value="14")
        ttk.Combobox(r2, textvariable=self._ana_days, values=["7","14","21","30","60"],
                     width=5, font=("PingFang SC", 12)).pack(side=tk.LEFT, padx=4)
        tk.Label(r2, text="天内成交（苹果建议14天）",
                 font=("PingFang SC", 11), fg="#888").pack(side=tk.LEFT, padx=8)

        btn_row = tk.Frame(tab); btn_row.pack(fill=tk.X, padx=14, pady=6)
        tk.Button(btn_row, text="📊 开始分析", font=("PingFang SC", 12),
                  bg="#722ed1", fg="white", padx=12,
                  command=self._run_analyze).pack(side=tk.LEFT)

        # 汇总卡片
        card_frm = tk.LabelFrame(tab, text=" 分析结果",
                                 font=("PingFang SC", 12, "bold"), padx=10, pady=6)
        card_frm.pack(fill=tk.X, padx=14, pady=(0, 4))
        cards = tk.Frame(card_frm); cards.pack(fill=tk.X)
        self._ana_labels = {}
        for key, label, color in [
            ("fast_p40","极速动销价\n(24h内P40)","#52c41a"),
            ("all_p50","保守参考价\n(全量P50)","#1890ff"),
            ("floor_p20","底价预警\n(全量P20)","#ff4d4f"),
            ("fast_ratio","极速动销率","#fa8c16"),
            ("avg_hours","平均动销时长","#722ed1"),
            ("total","样本量","#595959"),
        ]:
            col = tk.Frame(cards); col.pack(side=tk.LEFT, padx=14, pady=4)
            tk.Label(col, text=label, font=("PingFang SC", 10), fg="#888", justify="center").pack()
            lbl = tk.Label(col, text="—", font=("PingFang SC", 15, "bold"), fg=color)
            lbl.pack()
            self._ana_labels[key] = lbl
        self._ana_rec = tk.Label(card_frm, text="", font=("PingFang SC", 12), fg="#333",
                                 wraplength=750, justify="left")
        self._ana_rec.pack(anchor="w", pady=(4, 0))

        # 原始成交明细表
        tree_frm = tk.LabelFrame(tab, text=" 命中的历史成交明细（可核对每条数据）",
                                 font=("PingFang SC", 12, "bold"), padx=6, pady=6)
        tree_frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 4))
        cols = ("型号","成色","容量","颜色","成交价","动销时长","上架时间","成交时间")
        self._ana_tree = ttk.Treeview(tree_frm, columns=cols, show="headings", height=7)
        for col, w in zip(cols, [140, 90, 60, 80, 70, 80, 120, 120]):
            self._ana_tree.heading(col, text=col)
            self._ana_tree.column(col, width=w, anchor="center")
        asb = ttk.Scrollbar(tree_frm, orient="vertical", command=self._ana_tree.yview)
        self._ana_tree.configure(yscrollcommand=asb.set)
        asb.pack(side=tk.RIGHT, fill=tk.Y)
        self._ana_tree.pack(fill=tk.BOTH, expand=True)

        self._ana_log = self._make_log(tab, height=3)

    def _run_analyze(self):
        model = self._ana_model.get().strip()
        if not model:
            messagebox.showwarning("提示", "请选择或输入型号", parent=self._win); return
        self._log_clear(self._ana_log)
        for row in self._ana_tree.get_children(): self._ana_tree.delete(row)
        try: days = int(self._ana_days.get())
        except Exception: days = 14
        def _worker():
            self.root.after(0, lambda: self._log_append(self._ana_log, f"🔍 分析 {model} 近{days}天..."))
            try:
                analyzer = SoldAnalyzer()
                result, rows = analyzer.analyze_with_rows(
                    model, self._ana_cond.get().strip(),
                    self._ana_cap.get().strip(),
                    self._ana_color.get().strip(), days)
                self.root.after(0, lambda r=result, rs=rows: self._show_analyze_result(r, rs))
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self._log_append(self._ana_log, f"❌ 失败：{msg}"))
        threading.Thread(target=_worker, daemon=True).start()

    def _show_analyze_result(self, r: dict, rows: list):
        fmt = {
            "fast_p40":   f"¥{r['fast_p40']}" if r['fast_p40'] else "—",
            "all_p50":    f"¥{r['all_p50']}"  if r['all_p50']  else "—",
            "floor_p20":  f"¥{r['floor_p20']}" if r['floor_p20'] else "—",
            "fast_ratio": f"{r['fast_ratio']}%",
            "avg_hours":  f"{r['avg_hours']}h",
            "total":      f"{r['total']} 条",
        }
        for key, val in fmt.items():
            self._ana_labels[key].config(text=val)
        self._ana_rec.config(text=f"💡 {r['recommendation']}")
        # 填明细表，24h内成交的标绿
        for row in rows:
            tag = "fast" if row.get("fast") else ""
            self._ana_tree.insert("", tk.END, values=(
                row["model"], row["condition"], row["capacity"], row["color"],
                f"¥{row['price']:.0f}",
                f"{row['hours']:.1f}h" if row['hours'] >= 0 else "—",
                row["list_time"], row["sold_time"],
            ), tags=(tag,))
        self._ana_tree.tag_configure("fast", foreground="#52c41a")
        self._log_append(self._ana_log,
                         f"✅ 完成：{r['total']} 条，极速动销率 {r['fast_ratio']}%，均动销 {r['avg_hours']}h")

    # ── 通用工具 ──────────────────────────────────────────────────────────────
    def _make_log(self, parent, height=4):
        frm = tk.Frame(parent); frm.pack(fill=tk.X, padx=14, pady=(0, 6))
        sb  = ttk.Scrollbar(frm, orient="vertical")
        txt = tk.Text(frm, height=height, bg="#1e1e1e", fg="#a9b7c6",
                      font=("Menlo", 11), yscrollcommand=sb.set, padx=6, pady=4)
        sb.config(command=txt.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(fill=tk.X)
        return txt

    def _log_append(self, txt: tk.Text, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        txt.insert(tk.END, f"[{ts}] {msg}\n")
        txt.see(tk.END)

    def _log_clear(self, txt: tk.Text):
        txt.delete("1.0", tk.END)


if __name__ == "__main__":
    root = tk.Tk()
    app = PricingAssistantApp(root)
    root.mainloop()