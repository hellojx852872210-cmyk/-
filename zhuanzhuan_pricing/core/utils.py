"""
core/utils.py — 公共工具函数（无外部依赖）
"""
import re
import unicodedata
from typing import Optional

_BRAND_PREFIX_RE = re.compile(
    r"^(苹果|Apple|华为|HUAWEI|小米|MI|OPPO|VIVO|Redmi|荣耀|Honor|realme)\s*",
    re.IGNORECASE,
)
_STOP_WORDS_RE = re.compile(
    r"\s+(128G?B?|256G?B?|512G?B?|1TB?|8\+|12\+|16\+|黑|白|金|银|青|紫|红|蓝|钛|"
    r"全网通|移动版|电信版|联通版|官换|港版|美版|双卡|5G|4G).*$",
    re.IGNORECASE,
)
_MODEL_BRACKET_RE = re.compile(r"[\(（\[【].*?[\)）\]】]")
_MODEL_SEPARATOR_RE = re.compile(r"[|｜/]+")
_MODEL_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)
_MODEL_TOKEN_RE = re.compile(r"[0-9]+|[a-z]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_CAPACITY_COMBO_RE = re.compile(
    r"(?i)\b(?:8|12|16)\s*G?B?\s*\+\s*(?:128|256|512)\s*G?B?\b|\b(?:8|12|16)\s*G?B?\s*\+\s*1\s*T?B?\b"
)
_CAPACITY_RE = re.compile(r"(?i)\b(?:64|128|256|512)\s*G?B?\b|\b1\s*T?B?\b")
_COLOR_TOKENS = [
    "原色钛金属", "白色钛金属", "黑色钛金属", "蓝色钛金属",
    "深空黑", "暗紫色", "远峰蓝", "午夜色", "星光色", "石墨色",
    "银色", "金色", "紫色", "蓝色", "黑色", "白色", "绿色", "红色", "黄色", "粉色", "青色",
]


def clean_model_name(title: str) -> str:
    """去掉品牌前缀、规格后缀和常见噪音，尽量保留核心型号。"""
    raw = str(title or "").strip()
    if not raw:
        return ""
    text = _BRAND_PREFIX_RE.sub("", raw)
    text = _MODEL_BRACKET_RE.sub(" ", text)
    text = _MODEL_SEPARATOR_RE.sub(" ", text)
    text = _STOP_WORDS_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -_/：:")
    return text or raw


def normalize_model_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", clean_model_name(value)).casefold()
    text = _MODEL_NORMALIZE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_model_text(value: str) -> str:
    return "".join(_MODEL_TOKEN_RE.findall(normalize_model_text(value)))


def model_name_tokens(value: str) -> list[str]:
    return _MODEL_TOKEN_RE.findall(normalize_model_text(value))


def _contains_ordered_tokens(needle_tokens: list[str], haystack_tokens: list[str]) -> bool:
    """是否按顺序包含 needle tokens（允许中间有间隔）。"""
    if not needle_tokens:
        return True
    if not haystack_tokens:
        return False
    index = 0
    for token in haystack_tokens:
        if token == needle_tokens[index]:
            index += 1
            if index >= len(needle_tokens):
                return True
    return False


def _find_contiguous_tokens(needle_tokens: list[str], haystack_tokens: list[str]) -> tuple[int, int]:
    """返回连续 token 匹配区间 [start, end]，未命中返回 (-1, -1)。"""
    if not needle_tokens or not haystack_tokens:
        return -1, -1
    window = len(needle_tokens)
    for start in range(0, len(haystack_tokens) - window + 1):
        if haystack_tokens[start:start + window] == needle_tokens:
            return start, start + window - 1
    return -1, -1


def fuzzy_model_match(expected: str, candidate: str) -> bool:
    expected_norm = normalize_model_text(expected)
    candidate_norm = normalize_model_text(candidate)
    if not expected_norm or not candidate_norm:
        return False

    # 1) 归一化后完全一致
    if expected_norm == candidate_norm:
        return True

    expected_compact = compact_model_text(expected_norm)
    candidate_compact = compact_model_text(candidate_norm)

    # 2) 去分隔符后完全一致
    if expected_compact and candidate_compact and expected_compact == candidate_compact:
        return True

    expected_tokens = model_name_tokens(expected_norm)
    candidate_tokens = model_name_tokens(candidate_norm)
    if not expected_tokens or not candidate_tokens:
        return False

    # 3) token 完全一致（兼容输入中有无空格/分隔符）
    if expected_tokens == candidate_tokens:
        return True

    # 4) 多 token 查询仅允许“连续 token 精确命中 + 不允许更长后缀”
    # 例如允许 14pro 命中 [iphone, 14, pro]，但不允许命中 [iphone, 14, pro, max]。
    if len(expected_tokens) > 1:
        _start, end = _find_contiguous_tokens(expected_tokens, candidate_tokens)
        return end == len(candidate_tokens) - 1 if end >= 0 else False

    # 5) 仅对“单 token 查询”放宽模糊匹配（例如输入 14）
    if expected_norm in candidate_norm:
        return True
    if expected_compact and candidate_compact and expected_compact in candidate_compact:
        return True
    if _contains_ordered_tokens(expected_tokens, candidate_tokens):
        return True

    return False




def _normalize_capacity_token(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").upper())
    if not text:
        return ""
    if "+" in text:
        left, right = text.split("+", 1)
        if not left.endswith("GB"):
            left = left[:-1] + "GB" if left.endswith("G") else left + "GB"
        if right.endswith("TB"):
            right = right[:-2] + "T"
        elif right.endswith("GB"):
            right = right[:-2] + "G"
        return f"{left}+{right}"
    if text.endswith("TB"):
        return text[:-2] + "T"
    if text.endswith("GB"):
        return text[:-2] + "G"
    return text


def extract_capacity_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = _CAPACITY_COMBO_RE.search(text) or _CAPACITY_RE.search(text)
    return _normalize_capacity_token(match.group(0)) if match else ""


def extract_color_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for token in _COLOR_TOKENS:
        if token in text:
            return token
    return ""


def build_condition(properties: list | None = None, grade: dict | None = None) -> str:
    """优先从 properties 提取精确成色，缺失时再回退到 gradeInfo。"""
    for prop in properties or []:
        if str(prop.get("pnName") or "").strip() != "成色":
            continue
        condition = str(prop.get("pvName") or "").strip()
        if condition:
            return condition

    grade = grade or {}
    history = str(grade.get("historyGradeDesc") or "").strip()
    functional = str(grade.get("functionalGradeDesc") or "").strip()
    if history and functional:
        return f"{history} ({functional}级)"
    if history:
        return history
    if functional:
        return f"{functional}级"
    return "未知成色"


def round_to_8(price: int) -> int:
    """将价格调整为最近的以 8 结尾的整数（向下取整）"""
    remainder = price % 10
    return price - remainder + 8 if remainder >= 8 else price - remainder - 2


def estimate_settle(suggested_price: float, cur_settle: float,
                    cur_price: float,
                    platform_fee_rate: float = 0.05,
                    station_service_fee: float = 40) -> int:
    """估算改价后的到手价"""
    if cur_settle and cur_price:
        fee_rate = 1 - (cur_settle / cur_price)
    else:
        fee_rate = platform_fee_rate
    return int(round(suggested_price * (1 - fee_rate) - station_service_fee))


def safe_percentile(series, pct: float) -> int:
    """安全取分位数，空时返回 0"""
    if series is None or len(series) == 0:
        return 0
    try:
        return int(series.quantile(pct / 100))
    except Exception:
        return 0
