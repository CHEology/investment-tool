"""Explicit data-quality states (INV-7): never silently zero, never silently absent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class QualityState(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    ERROR = "ERROR"
    # Data usable for scanning but sourced from an unofficial/fallback provider.
    PROVISIONAL = "PROVISIONAL"


@dataclass
class Quality:
    state: QualityState
    detail: str = ""
    coverage: dict = field(default_factory=dict)

    @property
    def usable_for_scan(self) -> bool:
        return self.state in (QualityState.OK, QualityState.PARTIAL, QualityState.PROVISIONAL)

    @property
    def evidence_grade(self) -> bool:
        return self.state == QualityState.OK
