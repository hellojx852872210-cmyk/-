# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import requests

from ..core.models import ProductStatus


REQUIRED_COLUMNS = ["商品ID", "商品标题", "成本价"]
OPTIONAL_COLUMNS = ["商品状态", "质检码", "IMEI", "上架时间"]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass
class FeishuInventoryRow:
    product_id: str
    title: str
    cost_price: float
    status: ProductStatus
    qc_code: str
    imei: str
    listed_time: datetime.datetime | None


class FeishuInventoryService:
    def __init__(self, *, app_id: str, app_secret: str, app_token: str, table_id: str):
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.app_token = str(app_token or "").strip()
        self.table_id = str(table_id or "").strip()

    def validate_config(self) -> tuple[bool, str]:
        missing = []
        if not self.app_id:
            missing.append("feishu_inventory_app_id")
        if not self.app_secret:
            missing.append("feishu_inventory_app_secret")
        if not self.app_token:
            missing.append("feishu_inventory_app_token")
        if not self.table_id:
            missing.append("feishu_inventory_table_id")
        if missing:
            return False, f"missing config: {', '.join(missing)}"
        return True, "ok"

    def _tenant_token(self) -> str:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        data = resp.json() if resp.content else {}
        return str(data.get("tenant_access_token") or "")

    def _records_url(self) -> str:
        return f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"

    def fetch_all_rows(self) -> tuple[list[dict[str, Any]], str]:
        ok, reason = self.validate_config()
        if not ok:
            return [], reason
        token = self._tenant_token()
        if not token:
            return [], "tenant token empty"

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        page_token = ""
        rows: list[dict[str, Any]] = []

        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(self._records_url(), headers=headers, params=params, timeout=15)
            data = resp.json() if resp.content else {}
            if int(data.get("code", -1)) != 0:
                return [], f"bitable query failed: {data}"
            payload = data.get("data") or {}
            rows.extend(list(payload.get("items") or []))
            if not bool(payload.get("has_more", False)):
                break
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                break
        return rows, "ok"

    def validate_template(self) -> tuple[bool, str]:
        rows, reason = self.fetch_all_rows()
        if reason != "ok":
            return False, reason
        if not rows:
            return True, "ok"
        fields = rows[0].get("fields") if isinstance(rows[0], dict) else {}
        if not isinstance(fields, dict):
            fields = {}
        missing = [name for name in REQUIRED_COLUMNS if name not in fields]
        if missing:
            return False, f"missing required columns: {', '.join(missing)}"
        return True, "ok"

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _to_float(value: Any) -> float:
        if value in (None, ""):
            raise ValueError("empty")
        return float(value)

    @staticmethod
    def _to_status(raw: str) -> ProductStatus:
        text = str(raw or "").strip()
        if not text:
            return ProductStatus.ON_SALE
        mapping = {
            "在售": ProductStatus.ON_SALE,
            "已下架": ProductStatus.OFF_SALE,
            "未上架": ProductStatus.NOT_LISTED,
            "已售": ProductStatus.SOLD,
            "质检中": ProductStatus.IN_QC,
            "未知": ProductStatus.UNKNOWN,
            "0": ProductStatus.ON_SALE,
            "1": ProductStatus.OFF_SALE,
            "60": ProductStatus.NOT_LISTED,
            "80": ProductStatus.SOLD,
            "99": ProductStatus.IN_QC,
            "-1": ProductStatus.UNKNOWN,
        }
        return mapping.get(text, ProductStatus.UNKNOWN)

    @staticmethod
    def _to_datetime(raw: str) -> datetime.datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            return datetime.datetime.fromisoformat(text)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(text, fmt)
            except Exception:
                continue
        return None

    def parse_records(self, records: list[dict[str, Any]]) -> tuple[list[FeishuInventoryRow], list[str], bool, str]:
        if not records:
            return [], [], True, "ok"

        first_fields = records[0].get("fields") if isinstance(records[0], dict) else {}
        if not isinstance(first_fields, dict):
            first_fields = {}
        missing = [name for name in REQUIRED_COLUMNS if name not in first_fields]
        if missing:
            return [], [], False, f"missing required columns: {', '.join(missing)}"

        rows: list[FeishuInventoryRow] = []
        errors: list[str] = []
        for idx, record in enumerate(records, start=1):
            fields = record.get("fields") if isinstance(record, dict) else {}
            if not isinstance(fields, dict):
                errors.append(f"row#{idx}: fields invalid")
                continue
            product_id = self._text(fields.get("商品ID"))
            title = self._text(fields.get("商品标题"))
            cost_raw = fields.get("成本价")
            if not product_id:
                errors.append(f"row#{idx}: 商品ID empty")
                continue
            if not title:
                errors.append(f"row#{idx}: 商品标题 empty")
                continue
            try:
                cost_price = self._to_float(cost_raw)
            except Exception:
                errors.append(f"row#{idx}: 成本价 invalid ({cost_raw})")
                continue
            rows.append(
                FeishuInventoryRow(
                    product_id=product_id,
                    title=title,
                    cost_price=cost_price,
                    status=self._to_status(self._text(fields.get("商品状态"))),
                    qc_code=self._text(fields.get("质检码")),
                    imei=self._text(fields.get("IMEI")),
                    listed_time=self._to_datetime(self._text(fields.get("上架时间"))),
                )
            )
        return rows, errors, True, "ok"

    def fetch_and_parse(self) -> tuple[list[FeishuInventoryRow], list[str], bool, str]:
        records, reason = self.fetch_all_rows()
        if reason != "ok":
            return [], [], False, reason
        return self.parse_records(records)
