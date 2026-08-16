#!/usr/bin/env python3
"""
cadence.py. Spotify release-strategy X-ray.

BUILD STEP 1: auth + fetch + dedupe only.
Everything downstream depends on the dedupe being right, so this stage is
deliberately isolated and inspectable.

Hard constraint (Spotify deprecation, 2024-11-27): no audio-features,
audio-analysis, recommendations, related-artists, or preview_url. Verified 403
against this app's credentials. Everything here is built from release metadata.
"""

import os
import re
import sys
import json
import time
import base64
import unicodedata
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/Desktop/.env"))

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
MARKET = "US"                 # pinned for reproducibility; surfaced in UI

# VERIFIED CONSTRAINTS for these credentials (probed 2026-08-05, not assumed):
#   * page limit caps at 10, limit=20+ returns 400 "Invalid limit"
#   * /albums?ids= and /tracks?ids= batch endpoints return 403
#   * album_group is NOT returned on stubs, and `label`/`popularity` are absent
#     from every album/track object
#   * the `total` field is unreliable (Tyler reports 954, real count is 367)
#   * a combined include_groups query is flaky, returned 153 once and 0 the
#     next call. Querying each group separately is both accurate and stable.
PAGE_LIMIT = 10

# CORE SCOPE. `appears_on` (guest verses on other artists' records) is excluded
# deliberately: it is typically the majority of a catalog and therefore most of
# the request cost, and the daily quota is the binding constraint. Joint albums
# and collabs where the artist is credited still arrive under album/single, so
# what is actually lost is only the uncredited guest feature.
ALBUM_GROUPS = ("album", "single", "compilation")
GUEST_GROUP = "appears_on"

# Spotify has no EP type, so short projects have to be inferred from length.
# Anything Spotify calls an "album" with fewer than 8 tracks is treated as a
# short project rather than a full album, otherwise prolific artists who drop
# 5-track projects look like they release two albums a year.
EP_MIN_TRACKS = 4             # album_type=single with 4-6 tracks => inferred EP
EP_MAX_TRACKS = 6
ALBUM_MIN_TRACKS = 8


# ── Auth ─────────────────────────────────────────────────────────────────────

_token_cache = {"access_token": None, "expires_at": 0.0}


def get_token():
    """Client-credentials token, cached in memory until 60s before expiry."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError(
            "Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in ~/Desktop/.env"
        )

    auth = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + payload.get("expires_in", 3600)
    return _token_cache["access_token"]


# ── HTTP with rate-limit backoff ─────────────────────────────────────────────

class SpotifyError(Exception):
    pass


def api_get(path, params=None, _retries=0):
    """
    GET against the Spotify API. Honors Retry-After on 429 (rolling ~30s window).
    Refreshes the token once on a 401.
    """
    url = path if path.startswith("http") else f"{API}{path}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {get_token()}"},
        params=params,
        timeout=20,
    )

    if r.status_code == 429:
        if _retries >= 5:
            raise SpotifyError("Rate limited by Spotify after 5 retries.")
        wait = int(r.headers.get("Retry-After", "2")) + 1
        time.sleep(wait)
        return api_get(path, params, _retries + 1)

    if r.status_code == 401 and _retries == 0:
        _token_cache["access_token"] = None
        return api_get(path, params, _retries + 1)

    if r.status_code == 404:
        raise SpotifyError("Not found on Spotify. Double-check the link.")

    if r.status_code == 403:
        raise SpotifyError(
            f"Spotify returned 403 for {url}. This endpoint may be deprecated "
            "for apps without extended quota."
        )

    if not r.ok:
        raise SpotifyError(f"Spotify API error {r.status_code}: {r.text[:200]}")

    return r.json()


# ── Input parsing ────────────────────────────────────────────────────────────

_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_artist_id(raw):
    """
    Accept open.spotify.com/artist/{id} (with any query string),
    spotify:artist:{id}, or a bare 22-char ID.

    Names are deliberately rejected: search-by-name silently returns the wrong
    artist and every downstream number would be confidently incorrect.
    """
    if not raw or not raw.strip():
        raise ValueError("Paste a Spotify artist profile link to get started.")

    s = raw.strip()

    m = re.search(r"spotify:artist:([A-Za-z0-9]{22})", s)
    if m:
        return m.group(1)

    m = re.search(r"open\.spotify\.com/(?:intl-[a-z]{2}/)?artist/([A-Za-z0-9]{22})", s)
    if m:
        return m.group(1)

    if _ID_RE.match(s):
        return s

    if "open.spotify.com" in s and "/artist/" not in s:
        raise ValueError(
            "That's a Spotify link, but not an artist profile. Open the artist's "
            "page, tap the ••• menu, then Share → Copy link to artist."
        )

    raise ValueError(
        "Paste the artist's Spotify profile link, not their name. "
        "On the artist page: ••• menu → Share → Copy link to artist."
    )


# ── Fetch ────────────────────────────────────────────────────────────────────

def get_artist(artist_id):
    a = api_get(f"/artists/{artist_id}")
    return {
        "id": a["id"],
        "name": a.get("name", ""),
        "followers": (a.get("followers") or {}).get("total"),
        "popularity": a.get("popularity"),
        "genres": a.get("genres", []),
        "image": (a.get("images") or [{}])[0].get("url", ""),
        "url": (a.get("external_urls") or {}).get("spotify", ""),
    }


def _paginate_group(artist_id, group, progress=None):
    """
    Pull one album_group to exhaustion at 10/page.

    album_group is not returned in the payload, but include_groups still filters
    server-side, so querying each group separately and tagging the results
    reconstructs album_group exactly, with no inference. Verified: the four
    passes return zero overlap.
    """
    out = []
    url = f"/artists/{artist_id}/albums"
    params = {"limit": PAGE_LIMIT, "include_groups": group, "market": MARKET}

    while True:
        page = api_get(url, params)
        for item in page.get("items", []):
            out.append(_shape_stub(item, group))
        if progress:
            progress(group, len(out))
        nxt = page.get("next")
        if not nxt:
            break
        url, params = nxt, None      # `next` carries its own querystring
    return out


def get_all_release_stubs(artist_id, progress=None):
    """Full discography across all four groups. Never trusts the `total` field."""
    releases = []
    for group in ALBUM_GROUPS:
        releases.extend(_paginate_group(artist_id, group, progress))
    return releases


def _shape_stub(item, group):
    """
    Shape a release from the stub alone.

    `label` and `popularity` are absent from every object these credentials can
    reach, so nothing downstream may depend on them. Copyrights (which carry the
    label name in their text) come from the per-album enrichment pass below.
    """
    release_date = item.get("release_date", "") or ""
    precision = item.get("release_date_precision", "") or ""
    album_type = item.get("album_type", "") or ""
    total_tracks = item.get("total_tracks", 0) or 0
    artists = [a.get("name", "") for a in (item.get("artists") or [])]

    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "album_type": album_type,
        "album_group": group,
        "total_tracks": total_tracks,
        "release_date": release_date,
        "release_date_precision": precision,
        "release_year": _year_of(release_date),
        "url": (item.get("external_urls") or {}).get("spotify", ""),
        "image": (item.get("images") or [{}])[0].get("url", ""),
        "artists": artists,
        "primary_artist": artists[0] if artists else "",
        # Spotify has no EP type; 4-6 track "singles" are EPs in practice.
        "inferred_type": _infer_type(album_type, total_tracks),
        "date_is_approximate": precision in ("year", "month"),
        # filled by enrich_albums() for own albums only
        "upc": None,
        "copyrights": [],
        "tracks": [],
        "label_from_copyright": "",
        "date_may_be_reissue": False,
    }


def enrich_albums(releases, progress=None):
    """
    Fetch full objects to recover UPC + copyrights.

    The batch endpoint is 403 for this app, so this costs one request per album.
    That is only affordable for the artist's OWN albums and compilations, a
    dozen or so for most artists, not for hundreds of singles and guest spots.
    Singles keep name+tracks+year dedupe, which is sufficient for them.
    """
    targets = [r for r in releases if r["album_group"] in ("album", "compilation")]
    for i, rel in enumerate(targets):
        try:
            al = api_get(f"/albums/{rel['id']}", {"market": MARKET})
        except SpotifyError:
            continue
        copyrights = [c.get("text", "") for c in (al.get("copyrights") or [])]
        rel["upc"] = (al.get("external_ids") or {}).get("upc")
        rel["copyrights"] = copyrights
        rel["label_from_copyright"] = _label_from_copyright(copyrights)
        rel["date_may_be_reissue"] = _reissue_suspected(rel["release_date"], copyrights)
        # Tracklists ride along on the album fetch we already paid for, which is
        # what makes the single-to-album ramp free rather than one call per album.
        rel["tracks"] = [t.get("name", "") for t in ((al.get("tracks") or {}).get("items") or [])]
        if progress:
            progress("enrich", i + 1, len(targets))
    return releases


def _infer_type(album_type, total_tracks):
    """All EP classifications here are inferred from track count, and flagged
    as inferred in the UI. Spotify does not expose an EP type."""
    n = total_tracks or 0
    if album_type == "single" and EP_MIN_TRACKS <= n <= EP_MAX_TRACKS:
        return "ep"
    if album_type == "album" and 0 < n < ALBUM_MIN_TRACKS:
        return "ep"
    return album_type or "unknown"


def _year_of(release_date):
    m = re.match(r"^(\d{4})", release_date or "")
    return int(m.group(1)) if m else None


def _reissue_suspected(release_date, copyrights):
    """
    Cross-check the ℗ year against release_date. A 3+ year gap usually means the
    listed date is a reissue, not the original drop.
    """
    ry = _year_of(release_date)
    if not ry:
        return False
    years = []
    for text in copyrights:
        years += [int(y) for y in re.findall(r"(19\d{2}|20\d{2})", text or "")]
    if not years:
        return False
    return (ry - min(years)) >= 3


_COPYRIGHT_PREFIX_RE = re.compile(r"^\s*[\(\[]?\s*[PC℗©]?\s*[\)\]]?\s*(?:19|20)\d{2}\s*", re.I)
# The label often sits AFTER the licence wording, not before it:
#   "2011 Tyler, The Creator under exclusive license to XL Recordings Ltd"
# Here the artist's own entity owns the copyright and XL is the actual label.
_LICENSEE_RE = re.compile(
    r"\b(?:under exclusive licen[cs]e to|under licen[cs]e to|licen[cs]ed to|"
    r"distributed by|manufactured and distributed by|marketed by)\s+(.+)$", re.I)
_COPYRIGHT_TAIL_RE = re.compile(
    r"\s*,?\s*(a division of|an imprint of|a label of|under exclusive licen[cs]e to|"
    r"distributed by|manufactured and distributed by|as exclusive licensee|"
    r"marketed by|all rights reserved).*$", re.I)


def _label_from_copyright(copyrights):
    """
    Best-effort label from the ℗ line. The raw string is always kept and shown
    alongside, per the brief. The licence wording in it carries more signal
    than any single extracted name.
    """
    for text in copyrights:
        if not text:
            continue
        s = _COPYRIGHT_PREFIX_RE.sub("", text).strip()
        m = _LICENSEE_RE.search(s)
        if m:
            s = m.group(1)
        s = _COPYRIGHT_TAIL_RE.sub("", s).strip(" .,;-")
        s = re.sub(r"^(?:19|20)\d{2}\s+", "", s).strip(" .,;-")
        if s:
            return s
    return ""


# ── Dedupe ───────────────────────────────────────────────────────────────────

# Stripped for dedupe: pure market/format/explicit noise that does NOT change
# what the record IS. Deliberately does NOT strip Deluxe / Remix / Live /
# Anniversary. Those are genuine catalog extensions and section D needs to see
# them as separate releases.
_NOISE_TAG_RE = re.compile(
    r"""[\(\[\-\s]*\b(
        explicit(\s+version)? |
        clean(\s+version)? |
        (us|uk|eu|jp|japan|international|intl|worldwide)\s+(version|edition|release) |
        digital(\s+(version|release))? |
        stereo | mono |
        album\s+version |
        bonus\s+track\s+version
    )\b[\)\]]*""",
    re.I | re.X,
)


def normalize_name(name):
    """Lowercase, strip accents, drop market/format noise, collapse punctuation."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = _NOISE_TAG_RE.sub(" ", s)
    s = re.sub(r"\s*-\s*(single|ep)\s*$", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s.strip()


def dedupe_albums(albums):
    """
    Same record, many IDs: regional variants, re-issues, distributor migrations.

    1. Group by UPC where present. The authoritative identity.
    2. Otherwise group by normalized name + total_tracks + release year.

    Canonical pick within a group = earliest release_date (the original press,
    not the migration), tie-broken by higher popularity.

    Returns (deduped, report) so the collapse can be audited.
    """
    groups = {}
    for al in albums:
        if al.get("upc"):
            key = ("upc", al["upc"])
        else:
            key = ("nyt", normalize_name(al["name"]), al.get("total_tracks", 0), al.get("release_year"))
        groups.setdefault(key, []).append(al)

    deduped, collapsed = [], []
    for key, members in groups.items():
        members.sort(key=lambda a: (a.get("release_date") or "9999", -(a.get("popularity") or 0)))
        canonical = dict(members[0])
        if len(members) > 1:
            canonical["duplicate_ids"] = [m["id"] for m in members[1:]]
            collapsed.append({
                "kept": f'{canonical["name"]} ({canonical["release_date"]})',
                "matched_on": key[0],
                "dropped": [f'{m["name"]} ({m["release_date"]})' for m in members[1:]],
            })
        deduped.append(canonical)

    deduped.sort(key=lambda a: a.get("release_date") or "")
    report = {
        "raw_count": len(albums),
        "deduped_count": len(deduped),
        "collapsed_groups": collapsed,
    }
    return deduped, report


# ── Section A: cadence core ──────────────────────────────────────────────────

from statistics import median as _median, mean as _mean, pstdev as _pstdev

RAMP_WINDOW_DAYS = 180        # how far before an album to look for lead singles

# Catalog extensions: reissues of a record that already exists. They are not
# separate album cycles, so they must not each claim the same lead singles.
_EXTENSION_RE = re.compile(
    r"(deluxe|extended|anniversary|remix(es)?|\blive\b|acoustic|sped ?up|slowed|"
    r"instrumental(s)?|reissue|re-?issue|edition|bonus|the estate sale|\+\s*$|"
    r"\+\s*instrumental)", re.I)


def is_extension(name):
    return bool(_EXTENSION_RE.search(name or ""))


def _to_date(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _cadence_pool(releases, include_compilations=False):
    """
    Releases eligible for gap math.

    Year- and month-only dates are excluded from day-level gaps entirely, a
    2009 release stored as "2009" would otherwise imply a January 1 drop and
    fabricate a gap. Compilations are excluded by default because a label
    repackage is not an artist deciding to release something.
    """
    pool = []
    for r in releases:
        if r["release_date_precision"] != "day":
            continue
        if not include_compilations and r["album_group"] == "compilation":
            continue
        d = _to_date(r["release_date"])
        if d:
            pool.append((d, r))
    return sorted(pool, key=lambda x: x[0])


def compute_cadence(releases, include_compilations=False):
    """Section A. Median leads because one long hiatus wrecks the mean."""
    pool = _cadence_pool(releases, include_compilations)
    excluded = sum(1 for r in releases if r["release_date_precision"] != "day")

    by_type, by_year = {}, {}
    for r in releases:
        by_type[r["inferred_type"]] = by_type.get(r["inferred_type"], 0) + 1
        if r["release_year"]:
            by_year.setdefault(r["release_year"], 0)
            by_year[r["release_year"]] += 1

    base = {
        "release_count": len(releases),
        "by_type": by_type,
        "by_year": dict(sorted(by_year.items())),
        "approx_date_count": excluded,
        "enough_data": False,
    }
    if len(pool) < 3:
        return base

    # Same-day drops are one release event. Two singles on one Friday would
    # otherwise inject a 0-day gap and drag the median toward zero.
    days = sorted({d.date() for d, _ in pool})
    gaps = [(days[i] - days[i - 1]).days for i in range(1, len(days))]
    if not gaps:
        return base

    med = _median(gaps)
    avg = _mean(gaps)
    cv = (_pstdev(gaps) / avg) if avg else 0.0

    longest = max(gaps)
    li = gaps.index(longest)
    last = pool[-1][0]
    days_since = (datetime.utcnow() - last).days

    # Trend: recent pace vs career pace.
    cutoff = datetime.utcnow().replace(year=datetime.utcnow().year - 3)
    recent_days = sorted({d.date() for d, _ in pool if d >= cutoff})
    recent_gaps = [(recent_days[i] - recent_days[i - 1]).days
                   for i in range(1, len(recent_days))]
    recent_med = _median(recent_gaps) if len(recent_gaps) >= 2 else None
    trend, trend_pct = "Not enough recent data", None
    if recent_med is not None and med:
        trend_pct = round((recent_med - med) / med * 100)
        trend = ("Accelerating" if trend_pct <= -15 else
                 "Decelerating" if trend_pct >= 15 else "Holding steady")

    # Scarcity is an ALBUM behaviour. Measuring it on every release makes almost
    # everyone look prolific, because nearly all artists drop singles regularly.
    album_days = sorted({d.date() for d, r in pool
                         if r["inferred_type"] == "album" and not is_extension(r["name"])})
    album_gaps = [(album_days[i] - album_days[i - 1]).days
                  for i in range(1, len(album_days))]

    base.update({
        "album_count": len(album_days),
        "album_median_gap_days": round(_median(album_gaps)) if album_gaps else None,
        "enough_data": True,
        "release_events": len(days),
        "median_gap_days": round(med),
        "mean_gap_days": round(avg),
        "consistency_cv": round(cv, 2),
        "consistency_label": ("Metronomic" if cv < 0.5 else
                              "Fairly regular" if cv < 1.0 else
                              "Irregular" if cv < 1.6 else "Highly erratic"),
        "longest_drought_days": longest,
        "longest_drought_from": str(days[li]),
        "longest_drought_to": str(days[li + 1]),
        "days_since_last": days_since,
        "last_release_date": last.strftime("%Y-%m-%d"),
        "recent_median_gap_days": round(recent_med) if recent_med is not None else None,
        "trend": trend,
        "trend_pct": trend_pct,
        "first_release_date": pool[0][0].strftime("%Y-%m-%d"),
    })
    return base


def compute_rhythm(releases):
    """
    How often, and of what. The core question this tool exists to answer, split
    by release type so "a release every 24 days" doesn't hide the fact that
    albums come once every five months and singles fill the space between.
    """
    def gaps_for(pool):
        days = sorted({d.date() for d in pool})
        return [(days[i] - days[i-1]).days for i in range(1, len(days))]

    dated = []
    for r in releases:
        if r["release_date_precision"] != "day" or r["album_group"] == "compilation":
            continue
        d = _to_date(r["release_date"])
        if d:
            dated.append((d, r))
    if len(dated) < 2:
        return {"enough_data": False}

    dated.sort(key=lambda x: x[0])
    first, last = dated[0][0], dated[-1][0]
    years = max((last - first).days / 365.0, 0.5)

    singles = [d for d, r in dated if r["inferred_type"] in ("single", "ep")]
    albums = [d for d, r in dated
              if r["inferred_type"] == "album" and not is_extension(r["name"])]

    per_year = {}
    for d, _ in dated:
        per_year[d.year] = per_year.get(d.year, 0) + 1
    busiest = max(per_year.items(), key=lambda kv: kv[1]) if per_year else None

    sg, ag = gaps_for(singles), gaps_for(albums)
    return {
        "enough_data": True,
        "career_years": round(years, 1),
        "first_release": first.strftime("%Y-%m-%d"),
        "last_release": last.strftime("%Y-%m-%d"),
        "total": len(dated),
        "releases_per_year": round(len(dated) / years, 1),
        "single_count": len(singles),
        "singles_per_year": round(len(singles) / years, 1),
        "single_median_gap_days": round(_median(sg)) if sg else None,
        "album_count": len(albums),
        "albums_per_year": round(len(albums) / years, 1),
        "album_median_gap_days": round(_median(ag)) if ag else None,
        "releases_by_year": dict(sorted(per_year.items())),
        "busiest_year": busiest,
    }


# ── Section B: single-to-album lead time ─────────────────────────────────────

_FEAT_RE = re.compile(r"\s*[\(\[]?\s*(feat|ft|featuring|with)\.?\s+[^\)\]]*[\)\]]?\s*$", re.I)
_VERSION_RE = re.compile(r"\s*[\(\[][^\)\]]*(remix|version|edit|live|instrumental|"
                         r"acoustic|sped up|slowed|demo|mix)[^\)\]]*[\)\]]\s*", re.I)


def _norm_track(name):
    """Normalize a track title for matching a single against an album cut."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _VERSION_RE.sub(" ", s)
    s = _FEAT_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


def compute_ramp(releases):
    """
    Section B. The headline metric.

    For every album, find the singles released in the 180 days before it and
    check which ones actually landed on the record. Singles that did not make
    the album are the 'buffet': tracks floated to test reaction, then dropped.
    """
    albums, singles = [], []
    for r in releases:
        d = _to_date(r["release_date"])
        if not d or r["release_date_precision"] != "day":
            continue
        if (r["inferred_type"] == "album" and r["album_group"] == "album"
                and not is_extension(r["name"])):
            albums.append((d, r))
        elif r["inferred_type"] in ("single", "ep"):
            singles.append((d, r))

    albums.sort(key=lambda x: x[0])
    singles.sort(key=lambda x: x[0])

    per_album = []
    for adate, album in albums:
        window_start = adate - timedelta(days=RAMP_WINDOW_DAYS)
        in_window = [(sd, s) for sd, s in singles if window_start <= sd < adate]
        if not in_window:
            continue

        album_tracks = {_norm_track(t) for t in (album.get("tracks") or []) if t}
        lead, buffet = [], []
        for sd, s in in_window:
            # Singles are not enriched (that would cost a request each), so the
            # title is the signal. Double A-sides like "Who Dat Boy / 911" carry
            # two tracks in one title and must match on either side.
            names = list(s.get("tracks") or [])
            names.append(s["name"])
            names.extend(part for part in re.split(r"\s*/\s*", s["name"]) if part)
            matched = any(_norm_track(n) in album_tracks for n in names if n)
            entry = {"name": s["name"], "date": s["release_date"],
                     "days_before": (adate - sd).days}
            (lead if matched else buffet).append(entry)

        if not lead and not album_tracks:
            # No tracklist to match against, can't classify, so skip rather
            # than mislabel every single as a buffet track.
            continue

        lead_dates = sorted(_to_date(x["date"]) for x in lead)
        between = [ (lead_dates[i] - lead_dates[i-1]).days
                    for i in range(1, len(lead_dates)) ]

        per_album.append({
            "album": album["name"],
            "album_date": album["release_date"],
            "album_url": album["url"],
            "lead_single_count": len(lead),
            "lead_singles": sorted(lead, key=lambda x: -x["days_before"]),
            "buffet": sorted(buffet, key=lambda x: -x["days_before"]),
            "first_single_days_before": max((x["days_before"] for x in lead), default=None),
            "avg_gap_between_singles": round(_mean(between)) if between else None,
        })

    ramped = [a for a in per_album if a["lead_single_count"] > 0]
    with_days = [a for a in ramped if a["first_single_days_before"] is not None]
    recent_ramp = None
    if with_days:
        tail = with_days[-3:] if len(with_days) >= 3 else with_days[-2:]
        recent_ramp = round(_mean([a["first_single_days_before"] for a in tail]))

    career = {
        "recent_ramp_days": recent_ramp,
        "recent_ramp_basis": len(with_days[-3:]) if len(with_days) >= 3
                             else len(with_days[-2:]) if with_days else 0,
        "albums_analyzed": len(per_album),
        "albums_with_lead_singles": len(ramped),
        "avg_lead_singles": round(_mean([a["lead_single_count"] for a in ramped]), 1) if ramped else 0,
        "avg_ramp_days": round(_mean([a["first_single_days_before"] for a in ramped
                                      if a["first_single_days_before"] is not None]))
                          if ramped else None,
        "total_buffet_tracks": sum(len(a["buffet"]) for a in per_album),
    }
    return {"per_album": per_album, "career": career}


# ── Section D: catalog extension detection ───────────────────────────────────

_EXT_STRIP_RE = re.compile(
    r"\s*[\(\[]?\s*(deluxe|extended|anniversary|remix(es)?|live|acoustic|sped ?up|"
    r"slowed|instrumental(s)?|reissue|re-?issue|edition|bonus|the estate sale)"
    r"[^\)\]]*[\)\]]?\s*", re.I)


def _base_title(name):
    """Strip extension wording (and trailing '+' / ':' debris) to get the original title."""
    s = _EXT_STRIP_RE.sub(" ", name or "")
    s = re.sub(r"[\+:\-–-]+\s*$", " ", s)
    s = re.sub(r"\s*[\+&]\s*$", " ", s)
    return normalize_name(s)


def compute_extensions(releases):
    """
    Section D. How long does this artist keep working one record?

    Measures original release date -> final extension, which is the honest
    read on whether a catalog is worked or abandoned.
    """
    originals, extensions = [], []
    for r in releases:
        if r["inferred_type"] not in ("album", "ep") or r["album_group"] == "appears_on":
            continue
        (extensions if is_extension(r["name"]) else originals).append(r)

    by_base = {}
    for o in originals:
        by_base.setdefault(normalize_name(o["name"]), o)

    linked, orphans = [], []
    for ext in extensions:
        base = _base_title(ext["name"])
        parent = by_base.get(base)
        if parent is None:
            # fall back to longest-prefix match ("chromakopia" vs "chromakopiaplus")
            cands = [b for b in by_base if base and (base.startswith(b) or b.startswith(base))]
            parent = by_base[max(cands, key=len)] if cands else None
        if parent is None:
            orphans.append(ext)
            continue
        od, ed = _to_date(parent["release_date"]), _to_date(ext["release_date"])
        if not od or not ed:
            continue
        linked.append({
            "original": parent["name"], "original_date": parent["release_date"],
            "extension": ext["name"], "extension_date": ext["release_date"],
            "gap_days": (ed - od).days, "url": ext["url"],
        })

    linked.sort(key=lambda x: x["extension_date"])
    worked = {}
    for l in linked:
        worked[l["original"]] = max(worked.get(l["original"], 0), l["gap_days"])

    return {
        "original_album_count": len(originals),
        "extension_count": len(extensions),
        "linked": linked,
        "unmatched_extensions": [e["name"] for e in orphans],
        "avg_days_working_a_record": round(_mean(list(worked.values()))) if worked else None,
        "longest_worked": max(worked.items(), key=lambda kv: kv[1]) if worked else None,
        "extension_ratio": (round(len(extensions) / len(originals), 2)
                            if originals else None),
    }


# ── Section E: label trajectory ──────────────────────────────────────────────

_DISTRIBUTOR_TELLS = [
    "under exclusive license to", "under exclusive licence to",
    "distributed by", "manufactured and distributed",
    "marketed by", "under license to", "under licence to",
]


_LABEL_SUFFIX_RE = re.compile(
    r"\b(records?|recordings?|music|entertainment|group|inc|llc|ltd|limited|"
    r"co|company|corp|international)\b", re.I)


def _canonical_label(label):
    """
    Collapse per-release credit variations to the primary label.

    Larry June's copyrights cycle through "The Freeminded", "The Freeminded
    Records", "The Freeminded Records / EMPIRE", "The Freeminded / ALC / EMPIRE"
   . One label, nineteen spellings. Without this, every release reads as a
    label change and the trajectory becomes meaningless noise.
    """
    primary = re.split(r"\s*[/|]\s*", label or "")[0]
    key = _LABEL_SUFFIX_RE.sub(" ", primary)
    key = re.sub(r"^\s*the\s+", "", key, flags=re.I)
    key = re.sub(r"[^a-z0-9]+", "", key.lower())
    return key or normalize_name(primary), primary.strip()


def compute_labels(releases):
    """
    Section E. `label` is stripped for these credentials, so the ℗ line is the
    source. The raw string is surfaced verbatim. The distributor wording in it
    is usually the most revealing thing on the page.
    """
    timeline = []
    for r in sorted(releases, key=lambda x: x["release_date"] or ""):
        lab = r.get("label_from_copyright")
        if not lab:
            continue
        if is_extension(r["name"]):
            continue
        raw = " | ".join(r.get("copyrights") or [])
        tells = [t for t in _DISTRIBUTOR_TELLS if t in raw.lower()]
        timeline.append({
            "date": r["release_date"], "release": r["name"], "label": lab,
            "raw_copyright": raw, "distributor_tells": tells, "url": r["url"],
        })

    changes = []
    prev_key, prev_disp = None, None
    for e in timeline:
        key, disp = _canonical_label(e["label"])
        e["label_primary"] = disp
        if prev_key is None or key != prev_key:
            if prev_key is not None:
                changes.append({"date": e["date"], "from": prev_disp, "to": disp,
                                "release": e["release"]})
            prev_key, prev_disp = key, disp

    counts, seen_keys = {}, set()
    for e in timeline:
        key, disp = _canonical_label(e["label"])
        seen_keys.add(key)
        counts[disp] = counts.get(disp, 0) + 1

    return {
        "timeline": timeline,
        "changes": changes,
        "label_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "current_label": timeline[-1].get("label_primary") if timeline else None,
        "current_label_full": timeline[-1]["label"] if timeline else None,
        "label_count": len(seen_keys),
        "releases_with_label": len(timeline),
    }


# ── Section G: drop-day forensics ────────────────────────────────────────────

_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def compute_dropday(releases):
    """
    Section G. Friday is the global release standard; anything else is a
    deliberate choice, a non-standard distributor, or older catalog.
    """
    dow, months, quarters, non_friday = {}, {}, {}, []
    dated = 0
    for r in releases:
        if r["release_date_precision"] != "day":
            continue
        d = _to_date(r["release_date"])
        if not d:
            continue
        dated += 1
        name = _DOW[d.weekday()]
        dow[name] = dow.get(name, 0) + 1
        months[_MONTHS[d.month - 1]] = months.get(_MONTHS[d.month - 1], 0) + 1
        q = f"Q{(d.month - 1)//3 + 1}"
        quarters[q] = quarters.get(q, 0) + 1
        if d.weekday() != 4:
            non_friday.append({"date": r["release_date"], "day": name,
                               "release": r["name"], "type": r["inferred_type"]})

    lengths = {}
    for r in releases:
        if r["inferred_type"] == "album" and r["release_year"] and r["total_tracks"]:
            lengths.setdefault(r["release_year"], []).append(r["total_tracks"])
    length_trend = {y: round(_mean(v), 1) for y, v in sorted(lengths.items())}

    friday = dow.get("Friday", 0)
    return {
        "dated_releases": dated,
        "by_day": dict(sorted(dow.items(), key=lambda kv: -kv[1])),
        "by_month": {m: months.get(m, 0) for m in _MONTHS},
        "by_quarter": {f"Q{i}": quarters.get(f"Q{i}", 0) for i in range(1, 5)},
        "friday_pct": round(friday / dated * 100) if dated else 0,
        "non_friday": sorted(non_friday, key=lambda x: x["date"], reverse=True),
        "non_friday_count": len(non_friday),
        "album_length_by_year": length_trend,
    }


# ── Takeaways ────────────────────────────────────────────────────────────────
#
# Replaces the archetype/bucket idea, naming someone "The Architect" relabels
# the data without explaining it. Every takeaway is a complete sentence a person
# would actually say, and carries the numbers that prove it.
#
# Order is deliberate: how often they release, then what kind, then when in the
# week and year, then how albums get rolled out. Rhythm first, nuance after.

MIN_RELEASES_FOR_TAKEAWAYS = 5


def _tk(headline, detail, stat, weight, tag):
    return {"headline": headline, "detail": detail, "stat": stat,
            "weight": weight, "tag": tag}


def _plural(n, word):
    return f'{n} {word}{"" if n == 1 else "s"}'


def window_releases(releases, months=24):
    """Releases from the last N months, for the recent-behaviour view."""
    cutoff = datetime.utcnow() - timedelta(days=months * 30.4)
    out = []
    for r in releases:
        d = _to_date(r.get("release_date"))
        if d and d >= cutoff:
            out.append(r)
    return out


def build_recent_takeaways(releases, artist_name="This artist", months=24):
    """
    The same analysis restricted to recent history.

    Career-scale reads (label trajectory, longest drought, rollout drift) are
    deliberately left out. They need years of history to mean anything and would
    be misleading computed over a two-year slice.
    """
    recent = window_releases(releases, months)
    if len(recent) < 3:
        return {"takeaways": [], "summary":
                f'{artist_name} has released {_plural(len(recent), "time")} in the last '
                f"{months} months, too little to read a recent pattern."}

    c = compute_cadence(recent)
    ry = compute_rhythm(recent)
    rm = compute_ramp(recent)
    dd = compute_dropday(recent)
    res = build_takeaways(c, rm, {}, {}, dd, rhythm=ry, artist_name=artist_name,
                          limit=7, scope="recent")
    res["window_months"] = months
    res["window_count"] = len(recent)
    return res


def build_takeaways(cadence_stats, ramp, extensions, labels, dropday,
                    rhythm=None, artist_name="This artist", limit=9, scope="career"):
    """
    Ranked, evidence-backed reads on how and when an artist releases music.

    `scope` matters for correctness, not just tone. Every figure here is computed
    from whatever release set was passed in, so when that set is a 24-month
    window the wording must say so. Describing a windowed percentage as a share
    of "everything they have put out" produced a genuinely wrong number.
    """
    c, out = cadence_stats, []
    who = artist_name or "This artist"
    recent_scope = (scope == "recent")
    # Phrase every population reference against the set actually measured.
    span_txt = "in the last 24 months" if recent_scope else "across the catalogue"
    pop_txt = ("of everything released in this period" if recent_scope
               else f"of everything {who} has put out")

    if not c.get("enough_data") or c.get("release_count", 0) < MIN_RELEASES_FOR_TAKEAWAYS:
        return {"takeaways": [], "summary":
                f'{who} has only {c.get("release_count", 0)} releases with exact dates, '
                "not enough history to read a release strategy yet."}

    ry = rhythm or {}
    career = (ramp or {}).get("career", {})
    per_album = [a for a in (ramp or {}).get("per_album", []) if a["lead_single_count"]]

    # 1. How often, overall.
    if ry.get("enough_data"):
        implied = round(365 / c["median_gap_days"], 1) if c["median_gap_days"] else None
        clustered = (implied and ry["releases_per_year"]
                     and implied >= ry["releases_per_year"] * 1.4)
        if recent_scope:
            detail = (f'{_plural(ry["total"], "release")} between {ry["first_release"]} and '
                      f'{ry["last_release"]}, a rate of about {ry["releases_per_year"]} a year. ')
        else:
            detail = (f'{_plural(ry["total"], "release")} over {ry["career_years"]} years, '
                      f'from {ry["first_release"]} to {ry["last_release"]}, averaging '
                      f'{ry["releases_per_year"]} a year. ')
        if clustered:
            detail += (f'A {c["median_gap_days"]}-day gap on its own would suggest closer to '
                       f'{implied} releases a year. The difference is because releases arrive '
                       "in clusters rather than spaced evenly.")
        out.append(_tk(
            f'{who} releases something every {c["median_gap_days"]} days at their '
            "typical pace.",
            detail, f'{c["median_gap_days"]} days between releases', 100, "how often"))

        # 2. Singles.
        if ry.get("single_median_gap_days") and ry["single_count"] >= 3:
            gap = ry["single_median_gap_days"]
            detail = (f'{_plural(ry["single_count"], "single")} {span_txt}, '
                      f'a rate of about {ry["singles_per_year"]} a year. ')
            if gap and 365 / gap >= ry["singles_per_year"] * 1.5:
                detail += (f'A {gap}-day gap would imply more than that annually, so singles '
                           "arrive in clusters rather than evenly through the year. ")
            detail += ("Singles fill the space between albums, so this is the pace an "
                       "audience actually experiences.")
            out.append(_tk(f'{who} releases a single roughly every {gap} days.',
                           detail, f'a single every {gap} days', 95, "how often"))

        # 3. Albums.
        if ry.get("album_median_gap_days") and ry["album_count"] >= 3:
            gap = ry["album_median_gap_days"]
            period = (f'{round(gap/30.4)} months' if gap < 545 else f'{gap/365:.1f} years')
            implied_a = 365 / gap if gap else 0
            base = (f'{_plural(ry["album_count"], "album")} {span_txt}, a rate of about '
                    f'{ry["albums_per_year"]} a year. ')
            if implied_a >= ry["albums_per_year"] * 1.5:
                base += (f'Consecutive albums typically sit {gap} days apart, which alone '
                         f'would imply nearer {implied_a:.1f} a year. That gap between the two '
                         "figures is the tell: albums come in bursts with long quiet stretches "
                         "between them. ")
            else:
                base += f'Consecutive albums typically sit {gap} days apart. '
            base += (f'For contrast, the gap between releases of any kind is '
                     f'{c["median_gap_days"]} days.')
            out.append(_tk(f'A new album arrives about every {period}.',
                           base, f'an album every {period}', 92, "how often"))

        # 4. Busiest year. The share must be of the set measured, not the career.
        if ry.get("busiest_year") and ry["busiest_year"][1] >= 3:
            yr, n = ry["busiest_year"]
            share = round(n / ry["total"] * 100) if ry["total"] else 0
            out.append(_tk(
                f'{yr} was the busiest year, with {_plural(n, "release")}.',
                f'Output is not spread evenly. {yr} accounts for {share}% {pop_txt}.',
                f'{n} releases in {yr}', 60, "how often"))

    # 5. Day of the week.
    d = dropday or {}
    if d.get("dated_releases"):
        top_day, top_n = max(d["by_day"].items(), key=lambda kv: kv[1])
        if d.get("friday_pct", 100) < 70:
            alt = [(k, v) for k, v in d["by_day"].items() if k != "Friday"]
            alt_day, alt_n = max(alt, key=lambda kv: kv[1]) if alt else ("", 0)
            out.append(_tk(
                f'Only {d["friday_pct"]}% of releases came out on a Friday.',
                f'Friday is the worldwide standard release day and most artists stick to it. '
                f'{who} released on a different day {d["non_friday_count"]} times out of '
                f'{d["dated_releases"]}. The most common alternative is {alt_day}, used '
                f'{_plural(alt_n, "time")}.',
                f'{d["friday_pct"]}% on Fridays', 85, "when"))
        else:
            out.append(_tk(
                f'{d["friday_pct"]}% of releases came out on a Friday.',
                f'{who} sticks to the standard industry release day. {top_n} of '
                f'{d["dated_releases"]} releases landed on a Friday.',
                f'{d["friday_pct"]}% on Fridays', 70, "when"))

    # 6. Time of year.
    if d.get("by_quarter") and d.get("dated_releases", 0) >= 8:
        q, qn = max(d["by_quarter"].items(), key=lambda kv: kv[1])
        share = round(qn / d["dated_releases"] * 100)
        if share >= 33:
            qmonths = {"Q1": "January to March", "Q2": "April to June",
                       "Q3": "July to September", "Q4": "October to December"}[q]
            out.append(_tk(
                f'{share}% of releases land in {q} ({qmonths}).',
                f'{_plural(qn, "release")} of {d["dated_releases"]} fall in that window, '
                "a seasonal pattern rather than an even spread across the year.",
                f'{share}% in {q}', 55, "when"))

    # 7. Speeding up or slowing down. Compares recent years to the career, so it
    # is meaningless when the whole set is already a 24-month window.
    if not recent_scope and c.get("trend_pct") is not None and abs(c["trend_pct"]) >= 15:
        faster = c["trend_pct"] < 0
        out.append(_tk(
            f'{who} is releasing music {"more often" if faster else "less often"} than the '
            "career average.",
            f'Over the last three years the typical gap has been '
            f'{c["recent_median_gap_days"]} days, against {c["median_gap_days"]} days across '
            f'the whole career, {abs(c["trend_pct"])}% '
            f'{"quicker" if faster else "slower"}.',
            f'{abs(c["trend_pct"])}% {"faster" if faster else "slower"} lately', 88, "how often"))

    # 8. Steady or bursty.
    cv = c["consistency_cv"]
    if cv >= 1.3:
        out.append(_tk(
            "Releases come in bursts rather than on a steady schedule.",
            f'The gaps between releases vary widely around the {c["median_gap_days"]}-day '
            "midpoint. Expect several releases close together, then a quiet stretch, rather "
            "than a predictable drumbeat.",
            "bursty, not steady", 75, "how often"))
    elif cv <= 0.6:
        out.append(_tk(
            "The release schedule is unusually predictable.",
            f'Gaps stay close to the {c["median_gap_days"]}-day midpoint instead of swinging '
            "between long and short, a genuinely reliable clock.",
            "steady and predictable", 75, "how often"))

    # 9. Rollout.
    if career.get("avg_lead_singles") and career.get("avg_ramp_days"):
        n, days = career["avg_lead_singles"], career["avg_ramp_days"]
        out.append(_tk(
            f'Albums get about {n} lead single{"" if n == 1 else "s"}, starting {days} days '
            "before release.",
            f'Across {_plural(career["albums_with_lead_singles"], "album campaign")}, the '
            f'first single arrives roughly {days} days ahead of the album. That is the runway '
            "a record gets before it lands.",
            f'{days}-day album runway', 82, "rollout"))

    # 10. Rollout shift.
    if len(per_album) >= 3:
        first, last = per_album[0], per_album[-1]
        f, l = first["first_single_days_before"], last["first_single_days_before"]
        if f and l and f > 0 and l > 0 and (l <= f / 2 or f <= l / 2):
            tighter = l < f
            out.append(_tk(
                f'The album rollout has gotten {"much shorter" if tighter else "much longer"}, '
                f'from {f} days to {l}.',
                f'The first single ran {f} days ahead of "{first["album"]}" in '
                f'{first["album_date"][:4]}. By "{last["album"]}" in {last["album_date"][:4]} '
                f'that had {"shrunk" if tighter else "grown"} to {l} days. '
                + ("Long build-ups have been replaced by short, close-in launches."
                   if tighter else
                   "Campaigns are being built over a longer runway than before."),
                f'{f} days to {l} days', 90, "rollout"))

    # 11. Singles that never made an album.
    if career.get("total_buffet_tracks", 0) >= 1:
        names = [b["name"] for a in (ramp or {}).get("per_album", []) for b in a["buffet"]]
        out.append(_tk(
            f'{_plural(career["total_buffet_tracks"], "single")} released before an album '
            "never made the final tracklist.",
            "These came out during the run-up to a record and were then left off it: "
            + ", ".join(f'"{n}"' for n in names[:4])
            + ". Tracks like these are often used to test reaction before committing.",
            f'{career["total_buffet_tracks"]} left off the album', 58, "rollout"))

    # 12. Silence.
    if c.get("days_since_last", 0) >= 180:
        out.append(_tk(
            f'It has been {c["days_since_last"]} days since the last release.',
            f'The most recent release was {c["last_release_date"]}, already longer than the '
            f'usual {c["median_gap_days"]}-day gap.',
            f'{c["days_since_last"]} days quiet', 86, "when"))
    if c.get("longest_drought_days", 0) >= 365:
        where = "in this period" if recent_scope else "in the whole career"
        out.append(_tk(
            f'The longest gap between releases {where} was '
            f'{c["longest_drought_days"]} days ({c["longest_drought_days"]/365:.1f} years).',
            f'Nothing came out between {c["longest_drought_from"]} and '
            f'{c["longest_drought_to"]}.',
            f'{c["longest_drought_days"]}-day silence', 52, "when"))

    # 13. Working the catalogue.
    x = extensions or {}
    if x.get("avg_days_working_a_record"):
        out.append(_tk(
            f'Each album keeps getting worked for about '
            f'{x["avg_days_working_a_record"]} days after it comes out.',
            f'{_plural(x["extension_count"], "deluxe, reissue or remix release")} across '
            f'{_plural(x["original_album_count"], "original album")}. The longest was '
            f'"{x["longest_worked"][0]}", still being extended {x["longest_worked"][1]} days '
            "after its release.",
            f'{x["avg_days_working_a_record"]} days per record', 56, "catalog"))

    # 14. Album length drift.
    lt = (dropday or {}).get("album_length_by_year") or {}
    if len(lt) >= 3:
        yrs = sorted(lt)
        f, l = lt[yrs[0]], lt[yrs[-1]]
        if f and l and abs(l - f) / f >= 0.25:
            out.append(_tk(
                f'Albums have gotten {"shorter" if l < f else "longer"}, from about '
                f'{f:g} tracks to {l:g}.',
                f'Average album length was {f:g} tracks in {yrs[0]} and {l:g} in {yrs[-1]}.',
                f'{f:g} to {l:g} tracks', 50, "catalog"))

    # 15. Label path. Career-scale only.
    lb = labels or {}
    if lb.get("changes"):
        path = " to ".join([lb["changes"][0]["from"]] + [ch["to"] for ch in lb["changes"]])
        out.append(_tk(
            f'{who} has been on {_plural(lb["label_count"], "different label")}.',
            f'{path}. Currently on {lb["current_label"]}.',
            f'{len(lb["changes"])} label changes', 62, "business"))
    elif lb.get("current_label"):
        out.append(_tk(
            f'{who} has stayed on {lb["current_label"]} throughout.',
            f'No label change appears across '
            f'{_plural(lb["releases_with_label"], "release")} with copyright data.',
            lb["current_label"], 42, "business"))

    out.sort(key=lambda t: -t["weight"])
    top = out[:limit]
    return {"takeaways": top, "summary": " ".join(t["headline"] for t in top[:3]),
            "all_count": len(out), "scope": scope}


# ── Projection ───────────────────────────────────────────────────────────────

def project_next_12_months(cadence_stats, ramp):
    """
    Extrapolates this artist's own median gap forward. This is a projection of
    their established pattern, NOT a prediction of what they will actually do, labelled as such everywhere it surfaces.
    """
    if not cadence_stats.get("enough_data"):
        return {"windows": [], "note": "Not enough dated history to project a pattern."}

    med = cadence_stats.get("recent_median_gap_days") or cadence_stats["median_gap_days"]
    last = _to_date(cadence_stats["last_release_date"])
    if not last or not med:
        return {"windows": [], "note": "Not enough dated history to project a pattern."}

    horizon = datetime.utcnow() + timedelta(days=365)
    cursor = max(last, datetime.utcnow() - timedelta(days=med))
    windows = []
    while len(windows) < 12:
        cursor = cursor + timedelta(days=med)
        if cursor > horizon:
            break
        if cursor < datetime.utcnow():
            continue
        # +/- 25% of the gap expresses the artist's own irregularity
        slack = max(7, int(med * 0.25 * max(cadence_stats["consistency_cv"], 0.4)))
        windows.append({
            "center": cursor.strftime("%Y-%m-%d"),
            "from": (cursor - timedelta(days=slack)).strftime("%Y-%m-%d"),
            "to": (cursor + timedelta(days=slack)).strftime("%Y-%m-%d"),
            "month": cursor.strftime("%b %Y"),
        })

    cr = (ramp or {}).get("career", {})
    # Recent rollouts beat the career average, an artist who has compressed from
    # 83 days to 7 is not going to suddenly run a 33-day campaign.
    ramp_days = cr.get("recent_ramp_days") or cr.get("avg_ramp_days")
    basis = cr.get("recent_ramp_basis") or 0
    album_note = None
    if ramp_days and windows:
        target = (_to_date(windows[0]["center"]) + timedelta(days=ramp_days)).strftime("%b %Y")
        basis_txt = (f"their last {basis} album rollouts" if basis >= 2
                     else "their most recent album rollout")
        album_note = (f"Based on {basis_txt}, the first lead single now lands about "
                      f"{ramp_days} days before an album. So a single arriving in "
                      f"{windows[0]['month']} would point at an album around {target}.")

    return {
        "windows": windows,
        "based_on_gap_days": med,
        "uses_recent_pace": cadence_stats.get("recent_median_gap_days") is not None,
        "album_note": album_note,
        "note": ("Projected from this artist's own median gap between releases. "
                 "A pattern, not a prediction."),
    }


# ── Pipeline (step 1) ────────────────────────────────────────────────────────

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cadence_cache")
CACHE_TTL_HOURS = 24


def _cache_path(artist_id):
    return os.path.join(CACHE_DIR, f"{artist_id}.json")


def load_cached_discography(artist_id, max_age_hours=CACHE_TTL_HOURS):
    """Discographies do not change hourly, and the daily quota is the binding
    constraint, so never pay twice for the same artist inside the TTL."""
    p = _cache_path(artist_id)
    if not os.path.exists(p):
        return None
    if (time.time() - os.path.getmtime(p)) > max_age_hours * 3600:
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def save_cached_discography(artist_id, payload):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(artist_id), "w") as f:
            json.dump(payload, f)
    except OSError:
        pass


def fetch_discography(artist_url_or_id, progress=None, use_cache=True):
    """
    Enrichment runs BEFORE dedupe so the UPC tier is actually reachable.

    Enrichment only covers albums and compilations (one request each), so singles
    still dedupe on name+tracks+year. Enriching every single to get its UPC would
    cost one request per release, unaffordable against the daily quota, and
    unnecessary, since duplicate pressings cluster on albums rather than singles.
    """
    artist_id = parse_artist_id(artist_url_or_id)
    if use_cache:
        hit = load_cached_discography(artist_id)
        if hit:
            return hit

    artist = get_artist(artist_id)
    releases = get_all_release_stubs(artist_id, progress)
    enrich_albums(releases, progress)
    deduped, report = dedupe_albums(releases)
    payload = {"artist": artist, "releases": deduped, "dedupe_report": report}
    if use_cache:
        save_cached_discography(artist_id, payload)
    return payload


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "4V8LLVI7PbaPR0K2TGSxFF"  # Tyler, The Creator
    t0 = time.time()
    result = fetch_discography(target)
    rel, rep = result["releases"], result["dedupe_report"]

    by_group = {}
    for r in rel:
        by_group[r["album_group"]] = by_group.get(r["album_group"], 0) + 1

    print(f'\nARTIST: {result["artist"]["name"]}   ({time.time()-t0:.1f}s)')
    print(f'DEDUPE: {rep["raw_count"]} raw -> {rep["deduped_count"]} unique '
          f'({rep["raw_count"] - rep["deduped_count"]} collapsed across '
          f'{len(rep["collapsed_groups"])} groups)')
    print(f'GROUPS: {by_group}\n')

    if rep["collapsed_groups"]:
        print("SAMPLE COLLAPSES (audit these):")
        for g in rep["collapsed_groups"][:8]:
            print(f'  keep {g["kept"]}  [matched on {g["matched_on"]}]')
            for d in g["dropped"][:3]:
                print(f'       drop {d}')
        print()

    own = [r for r in rel if r["album_group"] != "appears_on"]
    print("DEDUPED DISCOGRAPHY (own releases, chronological):")
    print(json.dumps(own, indent=2))
