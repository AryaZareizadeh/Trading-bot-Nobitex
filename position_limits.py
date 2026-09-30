"""Nobitex margin position lifetime, from its published rule and position API.

The public market API does not publish a maximum lifetime. Refresh the help
article periodically for research; an open position's expirationDate is the
authority for live trading. Network failures retain the last known rule.
"""

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import re
import threading
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


HELP_URL = "https://nobitx.com/help/discover/position/how-long-can-i-extend-my-commitment-position/"
DEFAULT_MAX_HOLD_DAYS = 60  # Published 1405/03/16; used only if refresh fails.
_REFRESH_SECONDS = 6 * 60 * 60
_RETRY_SECONDS = 10 * 60
_TEHRAN = ZoneInfo("Asia/Tehran")
_lock = threading.Lock()
_days = DEFAULT_MAX_HOLD_DAYS
_next_refresh = 0.0
_verified_at = None
_last_refresh_ok = False


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_published_limit(html: str) -> int:
    """Accept only the article's explicit maximum for each margin order."""
    parser = _Text()
    parser.feed(html)
    content = " ".join(" ".join(parser.parts).split())
    content = content.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    match = re.search(r"هر\s+سفارش\s+تعهدی.{0,80}?حداکثر\s+تا\s+(\d{1,3})\s+روز", content)
    if not match:
        raise ValueError("Nobitex position lifetime was not found in the help article")
    days = int(match.group(1))
    if not 1 <= days <= 365:
        raise ValueError("Nobitex position lifetime is outside the expected range")
    return days


def current_max_hold_days() -> int:
    """Refresh at most every six hours, retaining a safe last-known value."""
    global _days, _next_refresh, _verified_at, _last_refresh_ok
    if time.monotonic() < _next_refresh:
        return _days
    with _lock:
        if time.monotonic() < _next_refresh:
            return _days
        try:
            request = Request(HELP_URL, headers={"User-Agent": "TradingBotNobitex/1.0"})
            with urlopen(request, timeout=5) as response:
                html = response.read(500_000).decode("utf-8")
            _days = parse_published_limit(html)
            _verified_at = datetime.now(timezone.utc).isoformat()
            _last_refresh_ok = True
            _next_refresh = time.monotonic() + _REFRESH_SECONDS
        except (OSError, UnicodeError, ValueError):
            _last_refresh_ok = False
            _next_refresh = time.monotonic() + _RETRY_SECONDS
        return _days


def published_limit_status() -> dict:
    """Expose whether the running process has verified its cached limit."""
    return {"days": _days, "verified_at": _verified_at, "documentation": HELP_URL,
            "fallback": _verified_at is None, "refresh_ok": _last_refresh_ok}


def exchange_expiration(value) -> datetime | None:
    """Parse the exchange expiry; date-only values start at Tehran midnight.

    Starting at midnight is conservative when the API omits a time of day.
    """
    if not value:
        return None
    try:
        raw = str(value).strip()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_TEHRAN if len(raw) == 10 else timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def should_close_before_expiry(position: dict, now: datetime | None = None) -> bool:
    expiry = exchange_expiration(position.get("expirationDate"))
    if expiry is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= expiry - timedelta(hours=1)
