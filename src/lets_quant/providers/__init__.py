from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Protocol


@dataclass(frozen=True)
class DailyBarsRequest:
    symbols: List[str]
    start_date: date
    end_date: date
    adjustment: str


@dataclass(frozen=True)
class ProviderPayload:
    content: bytes
    filename: str
    dataset: str
    provider: str
    provider_version: str
    request: Dict[str, Any]
    content_type: str = "text/csv"


class DailyBarsProvider(Protocol):
    name: str
    version: str

    def fetch_daily_bars(
        self, request: DailyBarsRequest
    ) -> ProviderPayload:
        ...
