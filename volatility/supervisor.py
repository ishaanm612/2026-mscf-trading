"""Session-boundary detection for supervised volatility workers.

The RIT server can leave a worker connected while a practice heat is stopped.
This module identifies the boundary between two heats without making requests or
submitting orders.  The command-line supervisor owns polling and process
management; ``run.py`` uses the same detector to exit its child worker cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SessionMarker:
    """The RIT values that identify one active competition heat.

    :param period: RIT period identifier, when supplied by the server.
    :param tick: Competition tick at the observation time.
    :param status: RIT case status at the observation time.
    """

    period: int | None
    tick: int
    status: str

    @classmethod
    def from_case(cls, case: Mapping[str, Any]) -> "SessionMarker":
        """Validate and normalize the case fields used for boundary checks.

        :param case: Raw ``/case`` response.
        :returns: A normalized marker.
        :raises ValueError: If the response does not contain a numeric tick.
        """

        tick = case.get("tick")
        period = case.get("period")
        if not isinstance(tick, (int, float)):
            raise ValueError("RIT case response is missing a numeric tick")
        if period is not None and not isinstance(period, (int, float)):
            raise ValueError("RIT case response has an invalid period")
        return cls(None if period is None else int(period), int(tick), str(case.get("status", "")))

    @property
    def is_active(self) -> bool:
        """Return whether this observation represents an active heat.

        :returns: ``True`` exactly for RIT's ``ACTIVE`` status.
        """

        return self.status == "ACTIVE"


class SessionBoundaryDetector:
    """Detect when a worker's originally active heat has ended or reset.

    A boundary is emitted after the worker has seen an active observation and
    then sees an inactive status, a different period, or a lower tick.  A lower
    tick matters because some practice resets retain the same period field.
    """

    def __init__(self) -> None:
        """Create a detector with no active heat assigned yet."""

        self._session: SessionMarker | None = None
        self._last_tick: int | None = None

    def observe(self, case: Mapping[str, Any]) -> bool:
        """Record one case observation and report whether its heat ended.

        The first inactive observation does not count as an end because a
        worker can start during a server transition.  Once an active heat has
        been observed, any inactive observation is a definite boundary.

        :param case: Raw ``/case`` response.
        :returns: ``True`` once the observed worker session has ended.
        """

        marker = SessionMarker.from_case(case)
        if self._session is None:
            if marker.is_active:
                self._session = marker
                self._last_tick = marker.tick
            return False
        if not marker.is_active:
            return True
        if marker.period != self._session.period:
            return True
        if self._last_tick is not None and marker.tick < self._last_tick:
            return True
        self._last_tick = marker.tick
        return False
