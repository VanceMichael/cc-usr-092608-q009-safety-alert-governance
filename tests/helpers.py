"""测试共享构造工具。"""

from __future__ import annotations

import itertools

from src.models import Source, SourceEvent, Scenario


_counter = itertools.count(1)


def make_event(
    source: Source,
    scenario: Scenario,
    observed_at: str,
    *,
    received_at: str | None = None,
    payload: dict | None = None,
    algorithm_version: str | None = None,
    location_hint: str | None = None,
    subject_ref: str | None = None,
    event_id: str | None = None,
    dedupe_key: str | None = None,
) -> SourceEvent:
    eid = event_id or f"EVT-{next(_counter):04d}"
    return SourceEvent(
        event_id=eid,
        source=source,
        scenario=scenario,
        dedupe_key=dedupe_key or f"{source.value}:{eid}",
        observed_at=observed_at,
        received_at=received_at or observed_at,
        payload=payload or {},
        algorithm_version=algorithm_version,
        location_hint=location_hint,
        subject_ref=subject_ref,
    )
