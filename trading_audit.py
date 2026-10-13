"""Append-only durable trading events and sanitized structured log records."""
import logging
import re
import uuid
from datetime import datetime, timezone

_LOGGER = logging.getLogger("trading.audit")
_AUTH = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:token|bearer|basic)\s+[^\s;,}]+")
_SECRET = re.compile(r"(?i)((?:[\w-]*(?:token|password|secret|api[_-]?key|credential)[\w-]*)[\"']?\s*[:=]\s*)[\"']?[^\s;,}\"']+[\"']?")
_URL_USER = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@", re.I)


def sanitized(value):
    value = _AUTH.sub(r"\1[redacted]", str(value))
    value = _SECRET.sub(r"\1[redacted]", value)
    return _URL_USER.sub(r"\1[redacted]@", value)


def audit_record(bot_id, event, detail=""):
    return {"bot_id": str(bot_id), "time": datetime.now(timezone.utc).isoformat(),
            "event": sanitized(event), "detail": sanitized(detail)}


def record_event(store, record, *, conn=None):
    if store is not None:
        if conn is None:
            store.put("audit_events", uuid.uuid4().hex, record)
        else:
            store.write(conn, "audit_events", uuid.uuid4().hex, record)
    _LOGGER.info("trading_event", extra=record)
