"""Permanent November-forward backlog plus Anthony's exact historical Lane 2 cohort."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

COVERAGE_START = "2026-11-01T07:00:00Z"
SCHEDULED_OUTCOME = "outcome_032DjlzDKpdXJZOzK4f7q3"


def field(name, condition, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "regular_field", "object_type": "activity.meeting", "field_name": name},
            "condition": condition}


def backlog_query(now):
    unresolved = {"type": "or", "queries": [field("outcome_id", {"type": "exists"}, True),
                  field("outcome_id", {"type": "reference", "reference_type": "outcome", "object_ids": [SCHEDULED_OUTCOME]})]}
    return {"type": "and", "queries": [{"type": "object_type", "object_type": "activity.meeting"},
            field("starts_at", {"type": "moment_range", "before": {"type": "fixed_utc", "value": now.isoformat()}}),
            {"type": "or", "queries": [
                {"type": "and", "queries": [unresolved, field("starts_at", {"type": "moment_range", "on_or_after": {"type": "fixed_utc", "value": COVERAGE_START}})]},
                field("starts_at", {"type": "moment_range", "on_or_after": {"type": "fixed_utc", "value": (now-timedelta(days=7)).isoformat()}})]}]}


def read_outcome_backlog(search_page, get_seed_page, fields, now):
    """Search cursors discover open obligations; the historical seed has no age limit."""
    rows, cursor, seen = {}, None, set()
    query = backlog_query(now)
    while True:
        body = search_page({"query": query, "_limit": 200, "cursor": cursor,
                            "_fields": {"activity.meeting": fields.split(",")}, "include_counts": True,
                            "sort": [{"direction": "asc", "field": {"type": "regular_field", "object_type": "activity.meeting", "field_name": "date_created"}}]})
        total = (body.get("count") or {}).get("total", 0)
        if total > 10000:
            raise RuntimeError("Outcome backlog exceeds Close search limit; partitioning required")
        for row in body.get("data", []):
            rows[row["id"]] = row
        cursor = body.get("cursor")
        if not cursor:
            break
        if cursor in seen:
            raise RuntimeError("Close outcome search repeated its cursor")
        seen.add(cursor)
    seed = json.loads(Path(__file__).with_name("lane2_outcome_backlog_seed.json").read_text())["meeting_ids"]
    for offset in range(0, len(seed), 100):
        body = get_seed_page({"id__in": ",".join(seed[offset:offset+100]), "_limit": 100, "_fields": fields})
        if body.get("has_more"):
            raise RuntimeError("Historical meeting seed read incomplete")
        for row in body.get("data", []):
            if row["id"] not in seed:
                raise RuntimeError("Historical meeting seed filter was ignored")
            rows[row["id"]] = row
    return list(rows.values())
