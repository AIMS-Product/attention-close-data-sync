"""Read only the requested Close meeting time window using native search cursors."""
from datetime import datetime, timezone


def read_meeting_window(search_page, fields, since, until):
    if since >= until:
        raise ValueError("Meeting window start must precede its end")
    query = {"type": "and", "queries": [
        {"type": "object_type", "object_type": "activity.meeting"},
        {"type": "field_condition", "field": {"type": "regular_field", "object_type": "activity.meeting", "field_name": "starts_at"},
         "condition": {"type": "moment_range", "on_or_after": {"type": "fixed_utc", "value": since.isoformat()},
                       "before": {"type": "fixed_utc", "value": until.isoformat()}}}]}
    meetings, cursor, seen = {}, None, set()
    while True:
        body = search_page({"query": query, "_limit": 200, "cursor": cursor,
                            "_fields": {"activity.meeting": fields.split(",")}, "include_counts": True,
                            "sort": [{"direction": "asc", "field": {"type": "regular_field", "object_type": "activity.meeting", "field_name": "date_created"}}]})
        if not isinstance(body.get("data"), list):
            raise RuntimeError("Close meeting search returned an incomplete page")
        if (body.get("count") or {}).get("total", 0) > 10000:
            raise RuntimeError("Requested meeting window exceeds Close search limit")
        for meeting in body["data"]:
            try:
                start = datetime.fromisoformat(meeting["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise RuntimeError("Close meeting search omitted a valid meeting start") from error
            if not since <= start < until:
                raise RuntimeError("Close meeting search returned an out-of-window meeting")
            meetings[meeting["id"]] = meeting
        cursor = body.get("cursor")
        if not cursor:
            return list(meetings.values())
        if cursor in seen:
            raise RuntimeError("Close meeting search repeated a cursor")
        seen.add(cursor)
