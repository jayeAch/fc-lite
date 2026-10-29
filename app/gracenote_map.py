"""Stub: no Gracenote CSV matching in the lite build."""
import re

_STATION_ID_RE = re.compile(r'^([A-Za-z]{2,3})(\d{4,})$')


def normalize_gracenote_id(value):
    raw = str(value or '').strip()
    if not raw:
        return None
    if raw.isdigit():
        return raw if len(raw) >= 5 else None
    m = _STATION_ID_RE.match(raw)
    return (m.group(1).upper() + raw[len(m.group(1)):]) if m else None


def resolve_gracenote(provider, *, upstream_id=None, lookup_key=None):
    return normalize_gracenote_id(upstream_id)
