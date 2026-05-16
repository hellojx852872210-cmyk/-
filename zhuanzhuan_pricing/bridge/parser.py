from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

BLOCK_MARKER = "【Claude B 回传】"
MIN_PLAIN_TEXT_LENGTH = 20


@dataclass
class ParsedReport:
    raw_block: str
    normalized_block: str
    wrapped_message: str
    marker_used: bool


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized


def stable_content_hash(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()


def extract_latest_report_block(text: str, allow_plain_text: bool = False) -> Optional[ParsedReport]:
    normalized = _normalize_text(text)
    if not normalized:
        return None

    marker_positions = [match.start() for match in re.finditer(re.escape(BLOCK_MARKER), normalized)]
    if marker_positions:
        start = marker_positions[-1]
        block = normalized[start:].strip()
        return ParsedReport(
            raw_block=block,
            normalized_block=block,
            wrapped_message="",
            marker_used=True,
        )

    if not allow_plain_text or len(normalized) < MIN_PLAIN_TEXT_LENGTH:
        return None

    return ParsedReport(
        raw_block=normalized,
        normalized_block=normalized,
        wrapped_message="",
        marker_used=False,
    )


def build_forward_message(parsed: ParsedReport, source_file: str | Path, forwarded_at: datetime) -> ParsedReport:
    source = Path(source_file)
    header = "\n".join(
        [
            "[Claude Bridge]",
            f"来源文件: {source}",
            f"转发时间: {forwarded_at.isoformat(timespec='seconds')}",
            "以下是 Claude B 最新回传:",
            "",
        ]
    )
    wrapped = f"{header}{parsed.normalized_block}".strip()
    return ParsedReport(
        raw_block=parsed.raw_block,
        normalized_block=parsed.normalized_block,
        wrapped_message=wrapped,
        marker_used=parsed.marker_used,
    )
