"""Voice Photo Search — bridge between HA/Extended OpenAI Conversation and Immich.

Endpoints:
- POST /api/search   — multi-mode search → populate Voice Search album
    Supports: CLIP visual search, person filter, date range, or any combination
- POST /api/restore  — restore album to random photo rotation
- GET  /health       — health check
"""

import os
import re
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests as http
import dateparser

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-photo-search")

IMMICH_URL = os.environ.get("IMMICH_URL", "http://localhost:2283")
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
ALBUM_ID = os.environ["VOICE_SEARCH_ALBUM_ID"]
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "20"))
DEFAULT_ALBUM_SIZE = int(os.environ.get("DEFAULT_ALBUM_SIZE", "250"))

HEADERS = {"x-api-key": IMMICH_API_KEY, "Content-Type": "application/json"}


# ── Immich API helpers ───────────────────────────────────────────────

def immich_smart_search(query: str, size: int = MAX_RESULTS) -> list[dict]:
    """CLIP-based visual similarity search."""
    resp = http.post(
        f"{IMMICH_URL}/api/search/smart",
        headers=HEADERS,
        json={"query": query, "size": size},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["assets"]["items"]


def immich_metadata_search(
    person_ids: list[str] | None = None,
    taken_after: str | None = None,
    taken_before: str | None = None,
    size: int = MAX_RESULTS,
) -> list[dict]:
    """Search by metadata: date range, person, or combination."""
    body: dict = {"size": size}
    if person_ids:
        body["personIds"] = person_ids
    if taken_after:
        body["takenAfter"] = taken_after
    if taken_before:
        body["takenBefore"] = taken_before
    resp = http.post(
        f"{IMMICH_URL}/api/search/metadata",
        headers=HEADERS,
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["assets"]["items"]


def resolve_person(name: str) -> dict | None:
    """Resolve a person name to an Immich person object (case-insensitive)."""
    resp = http.get(f"{IMMICH_URL}/api/people", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    name_lower = name.lower().strip()
    for p in people:
        if p.get("name", "").lower() == name_lower:
            return p
    for p in people:
        pname = p.get("name", "").lower()
        if pname and (name_lower in pname or pname in name_lower):
            return p
    return None


def get_known_people() -> list[str]:
    """Return list of known person names for error messages."""
    try:
        resp = http.get(f"{IMMICH_URL}/api/people", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return [p["name"] for p in resp.json().get("people", []) if p.get("name")]
    except Exception:
        return []


def immich_random_assets(count: int = DEFAULT_ALBUM_SIZE) -> list[str]:
    """Fetch random asset IDs. Immich caps at 250 per call."""
    all_ids = []
    remaining = count
    while remaining > 0:
        batch = min(remaining, 250)
        resp = http.get(
            f"{IMMICH_URL}/api/assets/random",
            headers=HEADERS,
            params={"count": batch},
            timeout=15,
        )
        resp.raise_for_status()
        ids = [a["id"] for a in resp.json()]
        all_ids.extend(ids)
        remaining -= len(ids)
        if len(ids) < batch:
            break
    return all_ids


def clear_album() -> int:
    album = http.get(f"{IMMICH_URL}/api/albums/{ALBUM_ID}", headers=HEADERS, timeout=10)
    album.raise_for_status()
    current_ids = [a["id"] for a in album.json().get("assets", [])]
    if current_ids:
        http.delete(
            f"{IMMICH_URL}/api/albums/{ALBUM_ID}/assets",
            headers=HEADERS,
            json={"ids": current_ids},
            timeout=10,
        )
    return len(current_ids)


def populate_album(asset_ids: list[str]) -> int:
    if not asset_ids:
        return 0
    resp = http.put(
        f"{IMMICH_URL}/api/albums/{ALBUM_ID}/assets",
        headers=HEADERS,
        json={"ids": asset_ids},
        timeout=10,
    )
    resp.raise_for_status()
    return sum(1 for r in resp.json() if r.get("success"))


def build_message(count, query="", person_names=None, date_from="", date_to=""):
    """Build a human-friendly response message."""
    parts = []
    if query:
        parts.append(f"matching '{query}'")
    if person_names:
        parts.append(f"of {' and '.join(person_names)}")
    if date_from and date_to and date_from == date_to:
        parts.append(f"from {date_from}")
    elif date_from and date_to:
        parts.append(f"from {date_from} to {date_to}")
    elif date_from:
        parts.append(f"after {date_from}")
    elif date_to:
        parts.append(f"before {date_to}")
    desc = " ".join(parts) if parts else ""
    if count == 0:
        return f"No photos found {desc}.".strip() if desc else "No photos found."
    return f"Loaded {count} photos {desc} to your picture frames. They will appear shortly."


def _get_people_cache() -> list[dict]:
    """Fetch and cache known people for the duration of a request."""
    if not hasattr(_get_people_cache, "_cache"):
        try:
            resp = http.get(f"{IMMICH_URL}/api/people", headers=HEADERS, timeout=10)
            resp.raise_for_status()
            _get_people_cache._cache = [
                p for p in resp.json().get("people", []) if p.get("name")
            ]
        except Exception:
            _get_people_cache._cache = []
    return _get_people_cache._cache


def detect_persons_in_query(query: str) -> tuple[list[dict], str]:
    """Detect known person names in a natural language query.

    Returns (matched_people, remaining_query) where remaining_query has
    person names and filler words like 'of', 'photos', 'pictures' stripped.
    """
    people = _get_people_cache()
    # Sort by name length descending so "Grand-Dad" matches before "Dad"
    people_sorted = sorted(people, key=lambda p: len(p["name"]), reverse=True)
    matched = []
    remaining = query
    for p in people_sorted:
        name = p["name"]
        # Word-boundary match, case-insensitive
        pattern = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)
        if pattern.search(remaining):
            matched.append(p)
            remaining = pattern.sub("", remaining)
    # Clean up residual filler words
    remaining = re.sub(
        r'\b(photos?|pictures?|images?|show|me|find|display|of|with|and|the|on|from|since|in|during|taken|frames?|picture)\b',
        '', remaining, flags=re.IGNORECASE
    )
    remaining = re.sub(r'\s+', ' ', remaining).strip()
    return matched, remaining


def detect_dates_in_query(query: str) -> tuple[str, str, str]:
    """Detect date references in a natural language query.

    Returns (date_from, date_to, remaining_query) with dates as YYYY-MM-DD.
    Handles: "from X to Y", "between X and Y", "on April 16",
    "yesterday", "last week", "last month", etc.
    """
    date_from = ""
    date_to = ""
    remaining = query

    # Try "from X to Y" or "between X and Y" patterns
    range_patterns = [
        r'from\s+(.+?)\s+to\s+(.+?)(?:\s|$)',
        r'between\s+(.+?)\s+and\s+(.+?)(?:\s|$)',
    ]
    for pat in range_patterns:
        m = re.search(pat, remaining, re.IGNORECASE)
        if m:
            d1 = dateparser.parse(m.group(1), settings={
                'PREFER_DATES_FROM': 'past', 'RELATIVE_BASE': datetime.now()
            })
            d2 = dateparser.parse(m.group(2), settings={
                'PREFER_DATES_FROM': 'past', 'RELATIVE_BASE': datetime.now()
            })
            if d1 and d2:
                date_from = d1.strftime("%Y-%m-%d")
                date_to = d2.strftime("%Y-%m-%d")
                remaining = remaining[:m.start()] + remaining[m.end():]
                return date_from, date_to, remaining.strip()

    # Try single date references
    date_phrases = [
        r'(yesterday)',
        r'(today)',
        r'(last\s+week)',
        r'(last\s+month)',
        r'(last\s+year)',
        r'(this\s+week)',
        r'(this\s+month)',
        r'(this\s+year)',
        r'(?:on|from|since)\s+(\w+\s+\d{1,2}(?:,?\s+\d{4})?)',
        r'(?:on|from|since)\s+(\d{1,2}\s+\w+(?:\s+\d{4})?)',
        r'(?:on|from|since)\s+(\d{4}-\d{2}-\d{2})',
        r'(\w+\s+\d{1,2}(?:,?\s+\d{4})?)',  # "April 16" without preposition
    ]
    for pat in date_phrases:
        m = re.search(pat, remaining, re.IGNORECASE)
        if m:
            parsed = dateparser.parse(m.group(1), settings={
                'PREFER_DATES_FROM': 'past', 'RELATIVE_BASE': datetime.now()
            })
            if parsed:
                phrase = m.group(1).lower()
                if 'week' in phrase:
                    # Start of that week (Monday) to end (Sunday)
                    start = parsed - timedelta(days=parsed.weekday())
                    end = start + timedelta(days=6)
                    date_from = start.strftime("%Y-%m-%d")
                    date_to = end.strftime("%Y-%m-%d")
                elif 'month' in phrase:
                    date_from = parsed.replace(day=1).strftime("%Y-%m-%d")
                    next_month = parsed.replace(day=28) + timedelta(days=4)
                    date_to = (next_month - timedelta(days=next_month.day)).strftime("%Y-%m-%d")
                elif 'year' in phrase:
                    date_from = parsed.replace(month=1, day=1).strftime("%Y-%m-%d")
                    date_to = parsed.replace(month=12, day=31).strftime("%Y-%m-%d")
                else:
                    # Single day
                    date_from = parsed.strftime("%Y-%m-%d")
                    date_to = date_from
                remaining = remaining[:m.start()] + remaining[m.end():]
                return date_from, date_to, remaining.strip()

    return date_from, date_to, remaining


def parse_natural_query(query: str) -> dict:
    """Parse a natural language query into structured search params.

    Returns dict with keys: person_ids, person_names, date_from, date_to, clip_query
    """
    # Reset people cache per request
    if hasattr(_get_people_cache, "_cache"):
        del _get_people_cache._cache

    matched_people, remaining = detect_persons_in_query(query)
    date_from, date_to, remaining = detect_dates_in_query(remaining)

    # Clean up remaining text for potential CLIP query
    remaining = re.sub(r'\s+', ' ', remaining).strip().strip('.,!?')

    return {
        "person_ids": [p["id"] for p in matched_people],
        "person_names": [p["name"] for p in matched_people],
        "date_from": date_from,
        "date_to": date_to,
        "clip_query": remaining,
    }


# ── Endpoints ────────────────────────────────────────────────────────

@app.route("/api/search", methods=["POST"])
def search_photos():
    body = request.get_json(silent=True) or {}
    query = body.get("query", "").strip()
    person_name = body.get("person", "").strip()
    date_from = body.get("date_from", "").strip()
    date_to = body.get("date_to", "").strip()

    if not query and not person_name and not date_from and not date_to:
        return jsonify({
            "error": "Provide at least 'query', 'person', or 'date_from'/'date_to'",
            "count": 0,
        }), 400

    # If only query is provided (HA single-param tool), parse it for person/date
    if query and not person_name and not date_from and not date_to:
        parsed = parse_natural_query(query)
        log.info("Parsed query '%s' → %s", query, {
            k: v for k, v in parsed.items() if v
        })
        if parsed["person_ids"] or parsed["date_from"]:
            # Natural language contained person/date → use metadata search
            person_ids = parsed["person_ids"]
            resolved_names = parsed["person_names"]
            date_from = parsed["date_from"]
            date_to = parsed["date_to"]
            clip_query = parsed["clip_query"]

            log.info("Metadata search: person=%s, date_from=%s, date_to=%s",
                     resolved_names, date_from, date_to)
            assets = immich_metadata_search(
                person_ids=person_ids or None,
                taken_after=f"{date_from}T00:00:00.000Z" if date_from else None,
                taken_before=f"{date_to}T23:59:59.999Z" if date_to else None,
            )
            asset_ids = [a["id"] for a in assets]

            if not asset_ids:
                msg = build_message(0, clip_query, resolved_names, date_from, date_to)
                return jsonify({"count": 0, "message": msg})

            removed = clear_album()
            added = populate_album(asset_ids)
            log.info("Search complete: found %d, removed %d old, added %d new",
                     len(asset_ids), removed, added)

            msg = build_message(added, clip_query, resolved_names, date_from, date_to)
            result = {"count": added, "message": msg}
            if clip_query:
                result["query"] = clip_query
            if resolved_names:
                result["person"] = resolved_names
            if date_from:
                result["date_from"] = date_from
            if date_to:
                result["date_to"] = date_to
            return jsonify(result)
        else:
            # Pure visual/scene query → CLIP search
            log.info("Smart search: %s", query)
            assets = immich_smart_search(query)
            asset_ids = [a["id"] for a in assets]

            if not asset_ids:
                return jsonify({"count": 0, "message": build_message(0, query)})

            removed = clear_album()
            added = populate_album(asset_ids)
            log.info("Search complete: found %d, removed %d old, added %d new",
                     len(asset_ids), removed, added)
            return jsonify({
                "count": added,
                "message": build_message(added, query),
                "query": query,
            })

    # Explicit structured params (direct API calls)
    person_ids = []
    resolved_names = []
    if person_name:
        for name in [n.strip() for n in person_name.split(",") if n.strip()]:
            person = resolve_person(name)
            if person:
                person_ids.append(person["id"])
                resolved_names.append(person["name"])
            else:
                return jsonify({
                    "error": f"Person '{name}' not found in your photos",
                    "count": 0,
                    "known_people": get_known_people(),
                }), 404

    # Decide search strategy: metadata if person/date, otherwise CLIP
    use_metadata = bool(person_ids or date_from or date_to)

    if use_metadata:
        log.info("Metadata search: person=%s, date_from=%s, date_to=%s",
                 resolved_names, date_from, date_to)
        assets = immich_metadata_search(
            person_ids=person_ids or None,
            taken_after=f"{date_from}T00:00:00.000Z" if date_from else None,
            taken_before=f"{date_to}T23:59:59.999Z" if date_to else None,
        )
    else:
        log.info("Smart search: %s", query)
        assets = immich_smart_search(query)

    asset_ids = [a["id"] for a in assets]

    if not asset_ids:
        msg = build_message(0, query, resolved_names, date_from, date_to)
        return jsonify({"count": 0, "message": msg})

    removed = clear_album()
    added = populate_album(asset_ids)

    log.info("Search complete: found %d, removed %d old, added %d new",
             len(asset_ids), removed, added)

    msg = build_message(added, query, resolved_names, date_from, date_to)
    result = {"count": added, "message": msg}
    if query:
        result["query"] = query
    if resolved_names:
        result["person"] = resolved_names
    if date_from:
        result["date_from"] = date_from
    if date_to:
        result["date_to"] = date_to
    return jsonify(result)


@app.route("/api/restore", methods=["POST"])
def restore_default():
    """Clear search results and fill album with random photos for normal rotation."""
    removed = clear_album()
    random_ids = immich_random_assets(DEFAULT_ALBUM_SIZE)
    added = populate_album(random_ids)
    log.info("Restored default: removed %d, added %d random", removed, added)
    return jsonify({
        "removed": removed,
        "added": added,
        "message": f"Picture frames restored to normal rotation with {added} random photos.",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8008)
