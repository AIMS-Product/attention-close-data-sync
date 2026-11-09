"""Pure decision rules for projecting Avoma outcome tags onto Close meetings."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone


COMPLETED_TAGS = {
    "show",
    "qualified",
    "disqualified",
    "one call close",
    "not interested",
    "lost",
    "closed lost",
    "decision made",
    "follow up scheduled",
}
NO_SHOW_TAGS = {"no show", "no show up", "ghost", "ghosted"}
RESCHEDULED_TAGS = {"reschedule", "rescheduled"}
CANCELLED_TAGS = {"cancelled", "canceled"}


def normalize_outcome_tag(label: object) -> str:
    """Normalize typography and whitespace without guessing at tag meaning."""
    if not isinstance(label, str):
        return ""
    normalized = unicodedata.normalize("NFKC", label).casefold()
    normalized = re.sub(r"[\u2010-\u2015\u2212_]+", " ", normalized)
    normalized = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def derive_meeting_outcome(label: object) -> str | None:
    """Return a native outcome key only for explicitly approved Avoma tags."""
    normalized = normalize_outcome_tag(label)
    if not normalized:
        return None
    if normalized in COMPLETED_TAGS:
        return "completed"
    if normalized in NO_SHOW_TAGS:
        return "no_show"
    if normalized in RESCHEDULED_TAGS:
        return "rescheduled"
    if normalized in CANCELLED_TAGS:
        return "cancelled"
    return None


def select_unique_close_meeting(
    meetings: list[dict],
    avoma_start: datetime,
    required_offer: str,
    offer_family_from_title,
    tolerance_hours: int,
    calendar_uid: str | None = None,
) -> tuple[dict | None, str]:
    """Match by lead, offer family, and time; never guess among candidates."""
    candidates = []
    cancelled_candidates = []
    if calendar_uid:
        identity_matches = [m for m in meetings if calendar_uid in (m.get("calendar_event_uids") or [])]
        if identity_matches:
            meetings = identity_matches
    for meeting in meetings:
        if offer_family_from_title(meeting.get("title") or "") != required_offer:
            continue
        raw_start = meeting.get("starts_at")
        if not raw_start:
            continue
        try:
            close_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        if close_start.tzinfo is None:
            close_start = close_start.replace(tzinfo=timezone.utc)
        gap = abs((close_start - avoma_start).total_seconds())
        if gap <= tolerance_hours * 3600:
            status = (meeting.get("status") or "").casefold()
            title = (meeting.get("title") or "").strip().casefold()
            if status in {"canceled", "cancelled"} or title.startswith(("canceled", "cancelled")):
                cancelled_candidates.append(meeting)
            else:
                candidates.append(meeting)

    # Prefer a live booking when an old canceled booking shares the same time
    # window. If there is no live booking, retain the sole canceled activity so
    # Avoma's cancellation tag can still map it to Cancelled/Rescheduled.
    if not candidates:
        candidates = cancelled_candidates

    if not candidates:
        return None, "close_match_missing"
    if len(candidates) != 1:
        return None, "close_match_ambiguous"
    return candidates[0], "matched"


def cancellation_outcome(
    matched_meeting: dict,
    lead_meetings: list[dict],
    required_offer: str,
    offer_family_from_title,
    is_supported_title,
) -> str:
    """A cancelled booking with a later same-offer booking is a reschedule."""
    matched_start = datetime.fromisoformat(
        matched_meeting["starts_at"].replace("Z", "+00:00")
    )
    if matched_start.tzinfo is None:
        matched_start = matched_start.replace(tzinfo=timezone.utc)
    for meeting in lead_meetings:
        if meeting.get("id") == matched_meeting.get("id"):
            continue
        title = meeting.get("title") or ""
        if not is_supported_title(title):
            continue
        if offer_family_from_title(title) != required_offer:
            continue
        if (meeting.get("status") or "").casefold() in {"canceled", "cancelled"} or (
            (meeting.get("title") or "").strip().casefold().startswith("canceled")
        ):
            continue
        raw_start = meeting.get("starts_at")
        if not raw_start:
            continue
        try:
            later_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        if later_start.tzinfo is None:
            later_start = later_start.replace(tzinfo=timezone.utc)
        if later_start > matched_start:
            return "rescheduled"
    return "cancelled"


def terminal_outcome_action(current_outcome: str | None, scheduled_outcome: str,
                            desired_outcome: str) -> str:
    """Describe a safe write decision after reading the current Close value."""
    if current_outcome == desired_outcome:
        return "already_in_sync"
    if current_outcome and current_outcome != scheduled_outcome:
        return "preserve_terminal"
    return "write"
