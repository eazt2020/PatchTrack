"""Reporting-period resolution.

Duplicated deliberately (rather than imported across service boundaries) so
each microservice remains independently deployable. The logic is identical
to ``compliance.engine.period_bounds`` and covered by the same test vector.
"""

from __future__ import annotations

from datetime import UTC, datetime


def period_bounds(
    reporting_month: str | None = None,
    quarter: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = now or datetime.now(UTC)
    if quarter:
        parts = quarter.upper().replace("-", " ").split()
        year = next((int(p) for p in parts if p.isdigit() and len(p) == 4), now.year)
        quarter_num = next(
            (int(p.replace("Q", "")) for p in parts if p.startswith("Q")), None
        )
        if quarter_num not in (1, 2, 3, 4):
            raise ValueError("quarter must look like 2026-Q3")
        start = datetime(year, 3 * (quarter_num - 1) + 1, 1, tzinfo=UTC)
        month_after = start.month + 3
        end = (
            datetime(year + 1, month_after - 12, 1, tzinfo=UTC)
            if month_after > 12
            else datetime(year, month_after, 1, tzinfo=UTC)
        )
        return start, end
    if reporting_month:
        try:
            start = datetime.strptime(reporting_month, "%Y-%m").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError("reporting_month must look like 2026-10") from exc
        end = (
            datetime(start.year + 1, 1, 1, tzinfo=UTC)
            if start.month == 12
            else datetime(start.year, start.month + 1, 1, tzinfo=UTC)
        )
        return start, end
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        datetime(start.year + 1, 1, 1, tzinfo=UTC)
        if start.month == 12
        else datetime(start.year, start.month + 1, 1, tzinfo=UTC)
    )
    return start, end
