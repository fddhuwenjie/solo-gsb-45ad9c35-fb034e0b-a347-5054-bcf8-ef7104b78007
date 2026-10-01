"""请求体验证（Pydantic v2）。体积字段用 Decimal，拒绝 float 精度问题。"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

WELL_RE = re.compile(r"^[A-Z]{1,2}\d{1,3}$")


def normalize_well(value: str) -> str:
    well = value.strip().upper()
    if not WELL_RE.match(well):
        raise ValueError("孔位标签须为字母+数字，如 A1、B12")
    return well


class WellCreate(BaseModel):
    """登记孔位中的初始样本与体积。"""

    well: str
    sample: str
    volume: Decimal = Field(gt=0)

    @field_validator("well")
    @classmethod
    def _well(cls, value: str) -> str:
        return normalize_well(value)

    @field_validator("sample")
    @classmethod
    def _sample(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("样本名不能为空")
        return value


class TransferOp(BaseModel):
    """一次移液：从 source 吸取 volume 注入 destination。"""

    source: str
    destination: str
    volume: Decimal = Field(gt=0)

    @field_validator("source", "destination")
    @classmethod
    def _well(cls, value: str) -> str:
        return normalize_well(value)

    @model_validator(mode="after")
    def _distinct_wells(self) -> "TransferOp":
        if self.source == self.destination:
            raise ValueError("来源孔与目标孔不能相同")
        return self


class BatchCreate(BaseModel):
    """一批移液操作，按 operations 顺序原子执行；request_key 用于幂等去重。"""

    request_key: str = Field(min_length=1, max_length=128)
    operations: list[TransferOp] = Field(min_length=1)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value


class ReversalCreate(BaseModel):
    """撤销一批已提交移液：request_key 用于撤销操作的幂等去重。"""

    request_key: str = Field(min_length=1, max_length=128)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value


class CapacityUpdate(BaseModel):
    """设置或取消孔位最大容量：正数表示上限，null 表示取消（不限容量）。"""

    max_capacity: Decimal | None = Field(default=None, gt=0)


def _case_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("case_id 不能为空")
    return value


class QuarantineCreate(BaseModel):
    """把一组已登记孔按案件隔离；request_key 用于隔离申请的幂等去重。"""

    wells: list[str] = Field(min_length=1, max_length=384)
    case_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)
    request_key: str = Field(min_length=1, max_length=128)

    @field_validator("wells")
    @classmethod
    def _wells(cls, values: list[str]) -> list[str]:
        # 归一化 + 去重（保留首次出现顺序），同一申请重复列出同一孔无意义
        seen: dict[str, None] = {}
        for value in values:
            seen.setdefault(normalize_well(value), None)
        return list(seen)

    @field_validator("case_id")
    @classmethod
    def _case(cls, value: str) -> str:
        return _case_id(value)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason 不能为空")
        return value

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value


class QuarantineReleaseCreate(BaseModel):
    """解除一组当前由该案件隔离的孔；request_key 用于解除操作的幂等去重。"""

    wells: list[str] = Field(min_length=1, max_length=384)
    case_id: str = Field(min_length=1, max_length=128)
    request_key: str = Field(min_length=1, max_length=128)

    @field_validator("wells")
    @classmethod
    def _wells(cls, values: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for value in values:
            seen.setdefault(normalize_well(value), None)
        return list(seen)

    @field_validator("case_id")
    @classmethod
    def _case(cls, value: str) -> str:
        return _case_id(value)

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_key 不能为空")
        return value
