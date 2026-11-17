"""Decision-time qualification provenance; no provider requests or inference."""
from datetime import datetime, timezone

QUALIFIED_FIELD = "cf_ZDx7NBQaDzV1yYrFcBMzt6cIYj81dAcswpNN0CQzCPS"
REASON_FIELD = 'cf_QqafXqVXlOszV67VKWYQ0R7L3OKZ5R8HDalq6tNYW7O'
CALL_ID_FIELD = 'cf_8R2sICBKksWuMxCyTQhe9lV18SmdqloPe11vNMLPLRZ'
DECISION_AT_FIELD = 'cf_HqNAefk0Ga2573X7QLk1P89iIGF8fawSApJOSfJeMkk'


def qualification_payload(value, reason, source_call_id, *, provider, decided_at=None):
    if value not in {"Yes", "No"}:
        raise ValueError("Qualification must be Yes or No")
    if provider not in {"avoma", "attention"}:
        raise ValueError("Qualification source provider is required")
    if not str(reason or "").strip() or not str(source_call_id or "").strip():
        raise ValueError("Qualification requires a reason and source call ID")
    at = decided_at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Decision time must have a timezone")
    return {
        f"custom.{QUALIFIED_FIELD}": value,
        f"custom.{REASON_FIELD}": f"{provider}: {reason.strip()}",
        f"custom.{CALL_ID_FIELD}": f"{provider}:{str(source_call_id).strip()}",
        f"custom.{DECISION_AT_FIELD}": at.astimezone(timezone.utc).isoformat(),
    }
