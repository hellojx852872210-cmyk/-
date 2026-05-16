from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class InterceptRow:
    imei: str
    account_name: str
    product_id: str
    qc_code: str
    title: str
    model: str
    status: str
    qc_item_id: str
    qc_item_name: str
    ori_qc_result: str
    post_qc_result: str
    flawed_photos_count: int

    def to_dict(self) -> dict:
        return asdict(self)
