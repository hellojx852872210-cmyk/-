from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***REDACTED***"
SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "webhook",
    "cookie",
    "authorization",
    "access_token",
    "refresh_token",
)
KEY_VALUE_PATTERNS = [
    re.compile(rf"(?i)\b({key})\b\s*[:=]\s*([^\s,;]+)")
    for key in SENSITIVE_KEYS
]
QUOTED_KEY_VALUE_PATTERNS = [
    re.compile(rf'(?i)([\"\']?{key}[\"\']?\s*[:=]\s*[\"\'])(.*?)([\"\'])')
    for key in SENSITIVE_KEYS
]
LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
URL_PATTERN = re.compile(r"https?://[^\s)\]>\"']+")


def _redact_key_values(text: str) -> str:
    redacted = text
    for pattern in KEY_VALUE_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", redacted)
    for pattern in QUOTED_KEY_VALUE_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", redacted)
    return redacted


def _redact_long_tokens(text: str) -> str:
    return LONG_TOKEN_PATTERN.sub(REDACTED, text)


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    parts = urlsplit(url)
    if not parts.query:
        return url
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if any(sensitive in key.lower() for sensitive in SENSITIVE_KEYS):
            query_items.append((key, REDACTED))
        else:
            query_items.append((key, value))
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))
    return cleaned


def redact_sensitive_text(text: str) -> str:
    redacted = _redact_key_values(text)
    redacted = URL_PATTERN.sub(_redact_url, redacted)
    redacted = _redact_long_tokens(redacted)
    return redacted
