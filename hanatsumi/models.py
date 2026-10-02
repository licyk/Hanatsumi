"""Tag 记录模型与 CSV 行编码。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hanatsumi.config import CATEGORY_NAMES

_REQUIRED_KEYS = ("id", "name", "post_count", "category", "is_deprecated", "created_at", "updated_at")


class TagDecodeError(ValueError):
    """tags.json 返回的字段不符合预期。"""


@dataclass(frozen=True, slots=True)
class Tag:
    id: int
    name: str
    post_count: int
    category: int
    is_deprecated: bool
    created_at: str
    updated_at: str
    words: tuple[str, ...]

    @property
    def category_name(self) -> str:
        return CATEGORY_NAMES.get(self.category, f"unknown({self.category})")

    @classmethod
    def from_api(cls, obj: Mapping[str, Any]) -> Tag:
        missing = [key for key in _REQUIRED_KEYS if key not in obj]
        if missing:
            raise TagDecodeError(f"缺少字段 {missing}: {obj!r}")
        raw_words = obj.get("words") or ()
        return cls(
            id=int(obj["id"]),
            name=str(obj["name"]),
            post_count=int(obj["post_count"]),
            category=int(obj["category"]),
            is_deprecated=bool(obj["is_deprecated"]),
            created_at=str(obj["created_at"]),
            updated_at=str(obj["updated_at"]),
            words=tuple(str(word) for word in raw_words),
        )

    def row(self) -> tuple[Any, ...]:
        """编码为 CSV 行；words 以空格拼接（tag 名本身不含空格）。"""
        return (
            self.id,
            self.name,
            self.post_count,
            self.category,
            "true" if self.is_deprecated else "false",
            self.created_at,
            self.updated_at,
            " ".join(self.words),
        )
