"""Timestamps: stored in UTC, shown in the viewer's time zone.

Everything tulda writes (sidecars, catalog, watch.json, watch-events.jsonl, job state) uses
UTC ISO 8601 with a `Z` suffix. Older watcher files hold local-offset times
(`2026-01-01T04:00:00-08:00`); `parse_ts` reads both.

Pages emit `<time class=lt datetime="...Z" title="... UTC">YYYY-MM-DD HH:MM UTC</time>`: the
text is a labelled UTC fallback that is what a browser with JavaScript off shows. LOCAL_JS
(an inline script allowed by its sha256 hash in the page CSP, see server.py) rewrites each one
to the browser's local time with the zone abbreviation and keeps the exact UTC in the title.

Must stay import-light: watchevents (read by the server) imports it."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from html import escape


def utc_iso(dt: datetime | None = None) -> str:
    """'2026-01-01T12:00:00Z' for now (or an aware datetime)."""
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_from_ts(ts: float) -> str:
    return utc_iso(datetime.fromtimestamp(ts, timezone.utc))


def parse_ts(s) -> datetime | None:
    """An aware UTC datetime from ISO 8601 with `Z` or a numeric offset; naive strings are
    taken as UTC (tulda never wrote naive local times). None if unparseable."""
    if not isinstance(s, str) or not s.strip() or len(s) > 40:
        return None
    t = s.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ts_key(s) -> str:
    """Sort/compare key for stored timestamps of either form ('' when unparseable)."""
    dt = parse_ts(s)
    return utc_iso(dt) if dt else ""


def time_html(s, missing: str = "-") -> str:
    """A <time> element for a stored timestamp (escaped text when unparseable)."""
    dt = parse_ts(s)
    if dt is None:
        return escape(missing if s in (None, "") else str(s), quote=True)
    z = utc_iso(dt)
    return (f'<time class=lt datetime="{z}" title="{dt.strftime("%Y-%m-%d %H:%M:%S")} UTC">'
            f'{dt.strftime("%Y-%m-%d %H:%M")} UTC</time>')


# Rewrites every time.lt[datetime] to local time. Also exposes tuldaTime(iso) -> <time>
# element for the gallery script, which builds its cards client-side.
LOCAL_JS = (
    "(function(){var F;try{F=new Intl.DateTimeFormat(undefined,{year:'numeric',month:'short',"
    "day:'numeric',hour:'2-digit',minute:'2-digit',timeZoneName:'short'})}catch(e){return}"
    "function u(d){return d.toISOString().replace('T',' ').slice(0,19)+' UTC'}"
    "function fix(t){var d=new Date(t.getAttribute('datetime'));if(isNaN(d))return;"
    "t.textContent=F.format(d);t.title=u(d)}"
    "window.tuldaTime=function(s){var t=document.createElement('time');t.className='lt';"
    "var d=new Date(s);if(isNaN(d)){t.textContent=s||'';return t}"
    "t.setAttribute('datetime',d.toISOString().replace(/\\.\\d{3}Z$/,'Z'));"
    "t.textContent=u(d).slice(0,16)+' UTC';fix(t);return t};"
    "function all(){document.querySelectorAll('time.lt[datetime]').forEach(fix)}"
    "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',all);else all()})();"
)


def script_hash(body: str) -> str:
    """CSP source expression for an inline <script> with exactly this text."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"


LOCAL_SCRIPT = f"<script>{LOCAL_JS}</script>"
LOCAL_HASH = script_hash(LOCAL_JS)
