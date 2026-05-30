#!/usr/bin/env python3
"""
fan_signal.py — YouTube comment analyzer for music artists.
Usage: python fan_signal.py "https://www.youtube.com/watch?v=VIDEO_ID"
       python fan_signal.py --compare "URL1" "URL2"
"""

import sys
import re
import json
import webbrowser
import os
import argparse
from collections import Counter

import requests
import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/Desktop/.env"))
load_dotenv()  # fallback: CWD or system .env

# ── Model config ─────────────────────────────────────────────────────────────
MODEL = "claude-haiku-4-5"
# Swap to "claude-sonnet-4-6" or "claude-opus-4-7" for richer analysis

# ── Constants ─────────────────────────────────────────────────────────────────
HEATMAP_BUCKETS = 40
MAX_COMMENTS = 3000
TIER_A_THRESHOLD = 5   # distinct timestamps → full heatmap
TIER_B_THRESHOLD = 1   # some timestamps → clip cards, no heatmap bars

REQUEST_BUCKETS = [
    "streaming", "full_version", "acoustic", "live", "tutorial", "merch", "lyrics"
]


# ── YouTube helpers ───────────────────────────────────────────────────────────

def extract_video_id(url: str):
    patterns = [
        r"(?:v=)([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"/shorts/([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def parse_iso_duration(duration: str) -> int:
    """Return total seconds from ISO-8601 duration string like PT3M12S."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    secs = int(m.group(3) or 0)
    return h * 3600 + mins * 60 + secs


def get_video_info(video_id: str, api_key: str):
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,contentDetails",
        "id": video_id,
        "key": api_key,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    if not items:
        return None
    item = items[0]
    snippet = item["snippet"]
    duration_str = item["contentDetails"]["duration"]
    return {
        "title": snippet.get("title", "Unknown Title"),
        "channel": snippet.get("channelTitle", "Unknown Channel"),
        "duration_seconds": parse_iso_duration(duration_str),
        "duration_str": duration_str,
    }


def fetch_comments(video_id, api_key, max_count=MAX_COMMENTS):
    """Fetch up to max_count comment dicts (top-level + replies).
    Each dict has keys: 'text' (str), 'likes' (int).
    """
    comments = []
    url = "https://www.googleapis.com/youtube/v3/commentThreads"
    params = {
        "part": "snippet,replies",
        "videoId": video_id,
        "maxResults": 100,
        "order": "relevance",
        "textFormat": "plainText",
        "key": api_key,
    }
    page_token = None

    while len(comments) < max_count:
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = requests.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            print(f"Network error fetching comments: {e}", file=sys.stderr)
            break

        # Comments disabled returns 403 with a specific error
        if resp.status_code == 403:
            err = resp.json().get("error", {})
            errors = err.get("errors", [])
            if errors and errors[0].get("reason") == "commentsDisabled":
                print("⚠️  Comments are disabled on this video.")
                return []
            resp.raise_for_status()

        if resp.status_code != 200:
            break

        data = resp.json()

        for thread in data.get("items", []):
            top_snip = thread["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "text": top_snip["textDisplay"],
                "likes": int(top_snip.get("likeCount", 0)),
                "author": top_snip.get("authorDisplayName", ""),
            })

            replies = thread.get("replies", {}).get("comments", [])
            for reply in replies:
                rsnip = reply["snippet"]
                comments.append({
                    "text": rsnip["textDisplay"],
                    "likes": int(rsnip.get("likeCount", 0)),
                    "author": rsnip.get("authorDisplayName", ""),
                })

            if len(comments) >= max_count:
                break

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return comments[:max_count]


# ── Heatmap (pure Python, no LLM) ────────────────────────────────────────────

def parse_timestamps(comments, duration_seconds):
    """Return Counter of {seconds: count} for valid timestamps in comments.
    comments is a list of dicts with a 'text' key, or plain strings.
    """
    ts_re = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
    counter = Counter()
    for comment in comments:
        text = comment["text"] if isinstance(comment, dict) else comment
        seen_in_comment = set()
        for m in ts_re.finditer(text):
            mins = int(m.group(1))
            secs = int(m.group(2))
            total = mins * 60 + secs
            # Discard timestamps that exceed the video duration
            if duration_seconds > 0 and total > duration_seconds:
                continue
            if total not in seen_in_comment:
                counter[total] += 1
                seen_in_comment.add(total)
    return counter


def build_heatmap_buckets(ts_counter, duration_seconds, n=HEATMAP_BUCKETS):
    """Return list of n bucket counts."""
    buckets = [0] * n
    if duration_seconds <= 0 or not ts_counter:
        return buckets
    bucket_size = duration_seconds / n
    for ts, count in ts_counter.items():
        idx = min(int(ts / bucket_size), n - 1)
        buckets[idx] += count
    return buckets


def build_heatmap_data(ts_counter, comments_list, duration_seconds, n=HEATMAP_BUCKETS):
    """Return (buckets, buckets_comments) where buckets_comments[i] is up to 3
    comment texts whose timestamps fall in bucket i, sorted by likes desc."""
    buckets = [0] * n
    # Each entry: list of (likes, text) tuples
    bucket_comment_pool = [[] for _ in range(n)]

    if duration_seconds <= 0 or not ts_counter:
        return buckets, [[] for _ in range(n)]

    bucket_size = duration_seconds / n
    ts_re = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")

    # Build ts -> bucket index map
    ts_to_bucket = {}
    for ts, count in ts_counter.items():
        idx = min(int(ts / bucket_size), n - 1)
        buckets[idx] += count
        ts_to_bucket[ts] = idx

    # For each comment, check if it contains a timestamp that maps to a bucket
    for comment in comments_list:
        text = comment["text"] if isinstance(comment, dict) else comment
        likes = comment["likes"] if isinstance(comment, dict) else 0
        seen_buckets = set()
        for m in ts_re.finditer(text):
            mins = int(m.group(1))
            secs = int(m.group(2))
            total = mins * 60 + secs
            if total in ts_to_bucket:
                bidx = ts_to_bucket[total]
                if bidx not in seen_buckets:
                    bucket_comment_pool[bidx].append((likes, text))
                    seen_buckets.add(bidx)

    # Keep top 3 by likes per bucket
    buckets_comments = []
    for pool in bucket_comment_pool:
        pool.sort(key=lambda x: x[0], reverse=True)
        buckets_comments.append([t for _, t in pool[:3]])

    return buckets, buckets_comments


def top_timestamps(ts_counter, n=5):
    """Return top n (seconds, count) pairs."""
    return ts_counter.most_common(n)


def fmt_time(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def fmt_time_in_comment(ts_seconds: int, text: str) -> bool:
    """Return True if the formatted timestamp string appears in the comment text."""
    ts_str = fmt_time(ts_seconds)
    return ts_str in text


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_with_claude(comments, top_ts):
    """
    Returns:
      clip_quotes: {seconds_str: verbatim_quote}
      requests: {bucket: {count, quote}}
      sentiment_positive_pct: int
      fan_vision: list of {quote, type}
    """
    if not comments:
        return {
            "clip_quotes": {},
            "requests": {},
            "sentiment_positive_pct": 0,
            "fan_vision": [],
        }

    # Build the comment block — cap at ~8000 chars for Haiku context
    # Include author so Claude can attribute fan_vision quotes
    def fmt_comment(c):
        if isinstance(c, dict):
            author = c.get("author", "")
            prefix = f"[{author}] " if author else ""
            return f"- {prefix}{c['text']}"
        return f"- {c}"
    comment_block = "\n".join(fmt_comment(c) for c in comments[:800])
    if len(comment_block) > 20000:
        comment_block = comment_block[:20000] + "\n[truncated]"

    # Describe top timestamps so Claude can find quotes
    ts_lines = "\n".join(f"  {fmt_time(ts)} ({cnt} mentions)" for ts, cnt in top_ts) or "  (none)"

    prompt = f"""You are analyzing YouTube comments for a music artist. Below are up to 400 fan comments.

TOP TIMESTAMPS fans mentioned (found by regex, not by you):
{ts_lines}

COMMENTS:
{comment_block}

Return ONLY valid JSON (no markdown fences, no preamble) with exactly this structure:
{{
  "clip_quotes": {{
    "<timestamp_as_M:SS>": "<verbatim quote from comments that references this moment>",
    ...
  }},
  "requests": {{
    "streaming": {{"count": <int>, "quote": "<verbatim quote or empty string>"}},
    "full_version": {{"count": <int>, "quote": "<verbatim quote or empty string>"}},
    "acoustic": {{"count": <int>, "quote": "<verbatim quote or empty string>"}},
    "live": {{"count": <int>, "quote": "<verbatim quote or empty string>"}},
    "tutorial": {{"count": <int>, "quote": "<verbatim quote or empty string>"}},
    "merch": {{"count": <int>, "quote": "<verbatim quote or empty string>"}},
    "lyrics": {{"count": <int>, "quote": "<verbatim quote or empty string>"}}
  }},
  "sentiment_positive_pct": <integer 0-100>,
  "fan_vision": [
    {{"quote": "<verbatim comment>", "type": "feature_wish|remix|sounds_like|production_note", "author": "<username if available or empty string>"}},
    ...
  ],
  "quotable_lines": [
    {{
      "line": "<the lyric or phrase being quoted>",
      "context": "<one sentence on why fans love it>",
      "count": <int — how many comments reference this line>,
      "comments": [
        {{"text": "<verbatim comment>", "author": "<username or empty string>"}},
        ...
      ]
    }},
    ...
  ]
}}

Rules:
- Only use the comment text provided. Never invent comments, counts, or quotes.
- Quotes must be copied verbatim from the input (exact words, no paraphrasing).
- For clip_quotes: for each timestamp listed above, find the single best verbatim comment that references that moment. If no comment clearly references it, use an empty string "".
- For requests: count how many comments fall into each bucket. A comment counts if it asks for something that doesn't exist yet (e.g. "put this on Spotify", "need the full version", "play this live"). Set count to 0 and quote to "" if bucket has no matches.
- For fan_vision: find up to 16 comments where fans project creative ideas onto the song — who should feature on it, remix suggestions, production style notes, artist comparisons ("this sounds like X"), imagined alternate versions. Only include comments that imagine something new about the song. Verbatim quotes only. Use type values: feature_wish, remix, sounds_like, or production_note.
- fan_vision should contain the most vivid, specific fan creative projections — only include comments that imagine something new about the song. Verbatim quotes only.
- For quotable_lines: find up to 10 specific lyrics, bars, or phrases that fans are directly quoting or referencing in the comments. These are lines fans copy-paste, put in quotes, or call out by name ("when he says...", "the line about..."). Sort by how many comments reference each line. Only include lines that genuinely appear to be from the song based on how fans quote them. For each line, include up to 4 verbatim comments (with author) that reference or quote it. If no lines are being quoted, return an empty array [].
- Return only valid JSON, nothing else."""

    client = anthropic.Anthropic()
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

        # Strip stray markdown fences
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

        result = json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        print(f"⚠️  Claude response parse error: {e}", file=sys.stderr)
        result = {}

    return {
        "clip_quotes": result.get("clip_quotes", {}),
        "requests": result.get("requests", {}),
        "sentiment_positive_pct": int(result.get("sentiment_positive_pct", 0)),
        "fan_vision": result.get("fan_vision", []),
        "quotable_lines": result.get("quotable_lines", []),
    }


# ── Tier determination ────────────────────────────────────────────────────────

def get_tier(distinct_ts_count: int) -> str:
    if distinct_ts_count >= TIER_A_THRESHOLD:
        return "A"
    elif distinct_ts_count >= TIER_B_THRESHOLD:
        return "B"
    return "C"


# ── HTML report ───────────────────────────────────────────────────────────────

def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def build_heatmap_html(buckets, duration_seconds, buckets_comments=None):
    if buckets_comments is None:
        buckets_comments = [[] for _ in range(len(buckets))]
    max_val = max(buckets) if any(buckets) else 1
    top3_indices = set(sorted(range(len(buckets)), key=lambda i: buckets[i], reverse=True)[:3])

    n = len(buckets)
    MAX_DOTS = 10

    cols_html = ""
    for i, val in enumerate(buckets):
        is_hot = i in top3_indices and val > 0
        filled_count = int(round((val / max_val) * MAX_DOTS)) if max_val else 0
        filled_count = min(filled_count, MAX_DOTS)

        # Skip empty columns entirely — no grey dots
        if filled_count == 0:
            ts_start_skip = fmt_time(int(i * duration_seconds / n))
            ts_end_skip = fmt_time(int((i + 1) * duration_seconds / n))
            cols_html += (
                f'<div class="dot-col empty" '
                f'data-ts="{ts_start_skip}–{ts_end_skip}" '
                f'data-count="0" data-cmts=""></div>'
            )
            continue

        ts_start = fmt_time(int(i * duration_seconds / n))
        ts_end = fmt_time(int((i + 1) * duration_seconds / n))

        cmts = buckets_comments[i] if i < len(buckets_comments) else []
        cmt_list = []
        for c in cmts[:2]:
            truncated = (c[:80] + "…") if len(c) > 80 else c
            cmt_list.append(html_escape(truncated))
        cmt_attr = "|||".join(cmt_list)

        col_class = "dot-col hot" if is_hot else "dot-col normal"
        dots = ""
        for d in range(filled_count):
            # d=0 is the top dot, d=filled_count-1 is the bottom
            if is_hot:
                delay = d * 0.08
                dots += f'<div class="dot-cell hot" style="animation-delay:{delay:.2f}s"></div>'
            else:
                dots += '<div class="dot-cell normal"></div>'

        cols_html += (
            f'<div class="{col_class}" '
            f'data-ts="{ts_start}–{ts_end}" '
            f'data-count="{val}" '
            f'data-cmts="{cmt_attr}">'
            f'{dots}'
            f'</div>'
        )

    # Time axis: 6 evenly spaced labels
    labels = ""
    for i in range(6):
        frac = i / 5
        ts = fmt_time(int(frac * duration_seconds))
        labels += f'<span style="flex:0 0 auto">{ts}</span>'

    return f"""<p class="section-desc">Each column shows where fans dropped timestamp comments in the song. The taller the column, the more fans reacted to that moment. Pulsing columns are peak fan activity.</p>
    <div class="heatmap-wrap">
      <div class="dot-grid" id="hm-bars">{cols_html}</div>
      <div class="time-axis">{labels}</div>
    </div>
    <div id="hm-tip" style="display:none;position:fixed;pointer-events:none;z-index:999;
      background:#ffffff;border:1px solid rgba(0,0,0,0.08);border-radius:12px;
      padding:12px 16px;font-size:12px;color:#1c1c1e;max-width:260px;line-height:1.6;
      box-shadow:0 4px 20px rgba(0,0,0,0.12);font-family:inherit;"></div>
    <script>
    (function(){{
      var tip = document.getElementById('hm-tip');
      var cols = document.querySelectorAll('.dot-col:not(.empty)');
      cols.forEach(function(col){{
        col.addEventListener('mouseenter', function(e){{
          var ts = col.dataset.ts || '';
          var cnt = col.dataset.count || '0';
          var cmtsRaw = col.dataset.cmts || '';
          var cmts = cmtsRaw ? cmtsRaw.split('|||') : [];
          var html = '<strong>' + ts + '</strong> &mdash; ' + cnt + ' mention' + (cnt === '1' ? '' : 's');
          cmts.forEach(function(c){{
            if(c) html += '<br><span style="color:#8e8e93;font-style:italic">&ldquo;' + c + '&rdquo;</span>';
          }});
          tip.innerHTML = html;
          tip.style.display = 'block';
        }});
        col.addEventListener('mouseleave', function(){{
          tip.style.display = 'none';
        }});
        col.addEventListener('mousemove', function(e){{
          var x = e.clientX + 12;
          var y = e.clientY - 10;
          if(x + 270 > window.innerWidth) x = e.clientX - 280;
          tip.style.left = x + 'px';
          tip.style.top = y + 'px';
        }});
      }});
    }})();
    </script>"""


def build_clip_cards_html(top_ts, clip_quotes, comments_at_ts=None):
    if not top_ts:
        return ""
    if comments_at_ts is None:
        comments_at_ts = {}
    cards = ""
    for ts, cnt in top_ts:
        label = fmt_time(ts)
        quote = clip_quotes.get(label, "")
        quote_html = f'<p class="quote">"{html_escape(quote)}"</p>' if quote else ""
        escaped_quote = html_escape(quote).replace("'", "&#39;")
        copy_btn = f"""<button class="copy-btn" onclick="navigator.clipboard.writeText('{escaped_quote}')">Copy</button>""" if quote else ""

        raw_cmts = comments_at_ts.get(label, [])
        raw_html = ""
        if raw_cmts:
            items = ""
            for rc in raw_cmts:
                truncated = (rc[:120] + "…") if len(rc) > 120 else rc
                items += f'<div class="raw-comment">"{html_escape(truncated)}"</div>'
            raw_html = f'<div class="raw-comments">{items}</div>'

        cards += f"""<div class="clip-card">
          <div class="ts-pill">{label}</div>
          <div class="clip-body">
            {quote_html}
            {raw_html}
            <p class="cluster-count">{cnt} comment{'s' if cnt != 1 else ''} cluster here</p>
            {copy_btn}
          </div>
        </div>"""
    return cards


def build_requests_html(requests_data: dict) -> str:
    labels = {
        "streaming": "Streaming (Spotify / Apple)",
        "full_version": "Full / Extended version",
        "acoustic": "Acoustic / Stripped",
        "live": "Tour / Live shows",
        "tutorial": "Chords / Tutorial / How you made it",
        "merch": "Merch",
        "lyrics": "Official lyrics",
    }
    # Filter out zero-count buckets, sort by count
    rows_data = [
        (k, v["count"], v.get("quote", ""))
        for k, v in requests_data.items()
        if isinstance(v, dict) and v.get("count", 0) > 0
    ]
    rows_data.sort(key=lambda x: x[1], reverse=True)

    if not rows_data:
        return '<p class="empty-state">No unmet fan requests detected in this comment section.</p>'

    rows = ""
    for rank, (bucket, count, quote) in enumerate(rows_data, 1):
        label = labels.get(bucket, bucket.replace("_", " ").title())
        quote_html = f'<span class="req-quote">"{html_escape(quote)}"</span>' if quote else ""
        rows += f"""<div class="req-row">
          <span class="req-rank">#{rank}</span>
          <span class="req-count">{count}</span>
          <div class="req-detail">
            <span class="req-label">{html_escape(label)}</span>
            {quote_html}
          </div>
        </div>"""
    return rows


def build_metric_cards_html(
    total_comments: int,
    clip_moments: int,
    unmet_requests: int,
    positive_pct: int,
    tier: str,
    top_request_label: str = "",
) -> str:
    clip_label = "Clip moments" if tier in ("A", "B") else "Top request"
    clip_value = str(clip_moments) if tier in ("A", "B") else (top_request_label or "—")

    cards = [
        ("Comments analyzed", str(total_comments), "#1db954"),
        (clip_label, clip_value, "#ff6b3d"),
        ("Unmet fan requests", str(unmet_requests), "#22c1a4"),
        ("% Positive", f"{positive_pct}%", "#6b6b7a"),
    ]
    html = ""
    for label, value, accent in cards:
        html += f"""<div class="metric-card">
          <div class="metric-value" style="color:{accent}">{html_escape(str(value))}</div>
          <div class="metric-label">{html_escape(label)}</div>
        </div>"""
    return html


def build_top_comments_html(comments, n=16):
    """Build HTML for top n most-liked comments."""
    if not comments:
        return '<p class="empty-state">No comments available.</p>'
    # Sort by likes descending
    sorted_cmts = sorted(comments, key=lambda c: c.get("likes", 0), reverse=True)
    top = sorted_cmts[:n]
    # Filter out 0-like comments if any have likes > 0
    if any(c.get("likes", 0) > 0 for c in top):
        top = [c for c in top if c.get("likes", 0) > 0][:n]
    if not top:
        return '<p class="empty-state">No liked comments found.</p>'
    cards = ""
    for c in top:
        text = c.get("text", "")
        likes = c.get("likes", 0)
        author = c.get("author", "")
        truncated = (text[:200] + "…") if len(text) > 200 else text
        author_html = f'<span class="vision-author">— {html_escape(author)}</span>' if author else ""
        cards += (
            f'<div class="comment-card">'
            f'<p class="comment-text">{html_escape(truncated)}</p>'
            f'<div class="comment-footer">'
            f'{author_html}'
            f'<span class="comment-likes"><span class="like-heart">&#9829;</span> {likes}</span>'
            f'</div>'
            f'</div>'
        )
    return cards


def build_fan_vision_html(fan_vision: list) -> str:
    """Render fan_vision items as cards with type badges."""
    if not fan_vision:
        return '<p class="empty-state">No fan creative projections detected.</p>'

    type_labels = {
        "feature_wish": "🎤 Feature wish",
        "remix": "🔄 Remix idea",
        "sounds_like": "🎵 Sounds like",
        "production_note": "🎛️ Production note",
    }

    cards = ""
    for item in fan_vision:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote", "")
        vtype = item.get("type", "")
        if not quote:
            continue
        badge_label = type_labels.get(vtype, vtype.replace("_", " ").title())
        author = item.get("author", "")
        author_html = f'<span class="vision-author">— {html_escape(author)}</span>' if author else ""
        cards += f"""<div class="vision-card">
          <span class="vision-badge">{badge_label}</span>
          <p class="vision-quote">"{html_escape(quote)}"</p>
          {author_html}
        </div>"""

    return cards if cards else '<p class="empty-state">No fan creative projections detected.</p>'


def build_quotable_lines_html(quotable_lines):
    if not quotable_lines:
        return ""
    items = ""
    for i, item in enumerate(quotable_lines):
        if not isinstance(item, dict):
            continue
        line = item.get("line", "")
        context = item.get("context", "")
        count = item.get("count", 0)
        fan_comments = item.get("comments", [])
        if not line:
            continue
        count_html = f'<span class="ql-count">{count} fan{"" if count == 1 else "s"} quoted this</span>' if count else ""

        comments_html = ""
        for fc in fan_comments:
            if not isinstance(fc, dict):
                continue
            ct = fc.get("text", "")
            au = fc.get("author", "")
            if not ct:
                continue
            author_span = f'<span class="ql-cmt-author">{html_escape(au)}</span>' if au else ""
            comments_html += f"""<div class="ql-comment">
              {author_span}
              <p class="ql-cmt-text">{html_escape(ct)}</p>
            </div>"""

        drawer_html = ""
        if comments_html:
            drawer_id = f"ql-drawer-{i}"
            drawer_html = f"""
            <div class="ql-drawer" id="{drawer_id}">
              <div class="ql-comments">{comments_html}</div>
            </div>"""

        has_drawer = bool(comments_html)
        card_class = "ql-card ql-expandable" if has_drawer else "ql-card"
        onclick = f' onclick="toggleQL(\'{drawer_id}\', this)"' if has_drawer else ""
        chevron = '<span class="ql-chevron">›</span>' if has_drawer else ""

        items += f"""<div class="{card_class}"{onclick}>
          <div class="ql-header">
            <p class="ql-line">"{html_escape(line)}"</p>
            {chevron}
          </div>
          <div class="ql-meta">
            <span class="ql-context">{html_escape(context)}</span>
            {count_html}
          </div>
          {drawer_html}
        </div>"""
    return items


def build_html_report(
    video_title,
    channel,
    duration_seconds,
    total_comments,
    tier,
    buckets,
    top_ts,
    clip_quotes,
    requests_data,
    sentiment_pct,
    comments=None,
    buckets_comments=None,
    fan_vision=None,
    comments_at_ts=None,
    quotable_lines=None,
):
    if quotable_lines is None:
        quotable_lines = []
    if fan_vision is None:
        fan_vision = []
    if comments_at_ts is None:
        comments_at_ts = {}

    unmet_count = sum(
        1 for v in requests_data.values()
        if isinstance(v, dict) and v.get("count", 0) > 0
    )
    top_req = ""
    if requests_data:
        top_bucket = max(
            ((k, v["count"]) for k, v in requests_data.items() if isinstance(v, dict)),
            key=lambda x: x[1], default=(None, 0)
        )
        labels_map = {
            "streaming": "Streaming", "full_version": "Full version",
            "acoustic": "Acoustic", "live": "Live", "tutorial": "Tutorial",
            "merch": "Merch", "lyrics": "Lyrics",
        }
        if top_bucket[0]:
            top_req = labels_map.get(top_bucket[0], top_bucket[0])

    metric_cards = build_metric_cards_html(
        total_comments, len(top_ts), unmet_count, sentiment_pct, tier, top_req
    )
    heatmap_html = build_heatmap_html(buckets, duration_seconds, buckets_comments) if tier == "A" else ""
    clip_cards = build_clip_cards_html(top_ts, clip_quotes, comments_at_ts) if tier in ("A", "B") else ""
    requests_html = build_requests_html(requests_data)
    top_comments_html = build_top_comments_html(comments or [])
    fan_vision_html = build_fan_vision_html(fan_vision)
    quotable_lines_html = build_quotable_lines_html(quotable_lines)

    # Build tab panels — only include tabs that have content
    tab_buttons = ""
    tab_panels = ""
    first_tab = True

    def add_tab(tab_id, label, content_html):
        nonlocal tab_buttons, tab_panels, first_tab
        active_btn = " active" if first_tab else ""
        active_panel = " active" if first_tab else ""
        first_tab = False
        tab_buttons += f'<button class="tab-btn{active_btn}" data-tab="{tab_id}">{label}</button>'
        tab_panels += f'<div class="tab-panel{active_panel}" id="{tab_id}">{content_html}</div>'

    if tier == "A":
        heatmap_section = f"""
          <h2 class="section-title">Reaction Heatmap <span class="section-sub">where fans talk about this track</span></h2>
          {heatmap_html}"""
        add_tab("tab-heatmap", "Heatmap", heatmap_section)

    if quotable_lines_html:
        ql_section = f"""
          <h2 class="section-title">Quotable Lines <span class="section-sub">lyrics fans can't stop repeating</span></h2>
          <p class="section-desc">Lines your fans are actively quoting in the comments — ready-made caption and marketing copy.</p>
          <div class="ql-grid">{quotable_lines_html}</div>"""
        add_tab("tab-quotes", "Quotable Lines", ql_section)

    vision_section = f"""
      <h2 class="section-title">Fan Vision <span class="section-sub">creative projections from fans</span></h2>
      <div class="vision-grid">{fan_vision_html}</div>"""
    add_tab("tab-vision", "Fan Vision", vision_section)

    top_comments_section = f"""
      <h2 class="section-title">Top Comments <span class="section-sub">most liked by fans</span></h2>
      <div class="top-comments-list">{top_comments_html}</div>"""
    add_tab("tab-comments", "Top Comments", top_comments_section)

    if tier in ("A", "B"):
        clip_section = f"""
          <h2 class="section-title">Top Moments to Clip</h2>
          <div class="clip-grid">{clip_cards}</div>"""
        add_tab("tab-clips", "Clip Moments", clip_section)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fan Signal — {html_escape(video_title)}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #f2f2f7;
    color: #1c1c1e;
    font-family: 'Inter', -apple-system, "SF Pro Text", "Segoe UI", sans-serif;
    font-size: 14px;
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: inherit; text-decoration: none; }}

  /* ── Header ── */
  .header {{
    background: rgba(255,255,255,0.85);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(0,0,0,0.06);
    padding: 32px 48px 28px;
  }}
  .tool-name {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: #ff6b3d;
    margin-bottom: 10px;
    opacity: 0.9;
  }}
  .video-title {{
    font-size: 24px;
    font-weight: 700;
    margin-bottom: 5px;
    color: #1c1c1e;
    letter-spacing: -0.3px;
  }}
  .video-meta {{
    font-size: 13px;
    color: #8e8e93;
    font-weight: 400;
  }}

  /* ── Metrics ── */
  .metrics {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    padding: 24px 48px 0;
  }}
  .metric-card {{
    background: #ffffff;
    border-radius: 16px;
    padding: 22px 24px 20px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
  }}
  .metric-value {{
    font-size: 34px;
    font-weight: 700;
    line-height: 1;
    margin-bottom: 6px;
    letter-spacing: -0.5px;
  }}
  .metric-label {{
    font-size: 11px;
    color: #8e8e93;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: .06em;
  }}

  /* ── Tab bar ── */
  .tab-bar {{
    display: flex;
    gap: 0;
    background: rgba(255,255,255,0.85);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(0,0,0,0.06);
    position: sticky;
    top: 0;
    z-index: 100;
    padding: 0 48px;
    margin-top: 20px;
  }}
  .tab-btn {{
    padding: 15px 18px;
    font-size: 13px;
    font-weight: 500;
    color: #8e8e93;
    border: none;
    background: none;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: color 0.2s, border-color 0.2s;
    font-family: inherit;
    letter-spacing: -0.1px;
  }}
  .tab-btn:hover {{ color: #1c1c1e; }}
  .tab-btn.active {{
    color: #1c1c1e;
    font-weight: 600;
    border-bottom-color: #ff6b3d;
  }}
  .tab-panel {{ display: none; padding: 32px 48px 60px; }}
  .tab-panel.active {{ display: block; }}

  /* ── Section titles ── */
  .section-title {{
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 6px;
    color: #1c1c1e;
    letter-spacing: -0.2px;
    display: flex;
    align-items: baseline;
    gap: 10px;
  }}
  .section-sub {{
    font-size: 13px;
    font-weight: 400;
    color: #8e8e93;
    letter-spacing: 0;
  }}
  .section-desc {{
    font-size: 13px;
    color: #8e8e93;
    margin-bottom: 24px;
    line-height: 1.6;
    font-weight: 400;
  }}

  /* ── Heatmap ── */
  .heatmap-wrap {{
    background: #ffffff;
    border-radius: 16px;
    padding: 28px 24px 20px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
  }}
  @keyframes blink {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.2; }}
  }}
  .dot-grid {{
    display: flex;
    align-items: flex-end;
    gap: 4px;
    margin-bottom: 16px;
    min-height: 140px;
    padding-top: 20px;
  }}
  .dot-col {{
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    gap: 3px;
    cursor: default;
  }}
  .dot-col.empty {{ pointer-events: none; }}
  .dot-cell {{
    width: 100%;
    aspect-ratio: 1;
    border-radius: 50%;
  }}
  .dot-cell.normal {{ background: #ff6b3d; opacity: 0.8; }}
  .dot-cell.hot {{
    background: #ff6b3d;
    animation: blink 0.85s ease-in-out infinite;
  }}
  .time-axis {{
    display: flex;
    justify-content: space-between;
    font-size: 10px;
    color: #8e8e93;
    font-weight: 500;
    letter-spacing: .04em;
    padding-top: 12px;
    border-top: 1px solid rgba(0,0,0,0.05);
  }}

  /* ── Clip cards ── */
  .clip-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
  }}
  .clip-card {{
    background: #ffffff;
    border-radius: 16px;
    padding: 20px 22px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
  }}
  .ts-pill {{
    display: inline-flex;
    align-items: center;
    background: rgba(255,107,61,0.1);
    color: #ff6b3d;
    font-size: 12px;
    font-weight: 700;
    border-radius: 8px;
    padding: 4px 10px;
    margin-bottom: 12px;
    letter-spacing: .02em;
  }}
  .clip-body {{ display: flex; flex-direction: column; gap: 10px; }}
  .quote {{
    font-size: 13px;
    color: #1c1c1e;
    font-style: italic;
    line-height: 1.55;
  }}
  .cluster-count {{
    font-size: 11px;
    color: #8e8e93;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: .05em;
  }}
  .copy-btn {{
    align-self: flex-start;
    background: #f2f2f7;
    border: none;
    color: #3a3a3c;
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    font-family: inherit;
    transition: background 0.15s;
  }}
  .copy-btn:hover {{ background: #e5e5ea; }}

  /* ── Raw comments in clip cards ── */
  .raw-comments {{ margin-top: 2px; display: flex; flex-direction: column; gap: 8px; }}
  .raw-comment {{
    font-size: 12px;
    color: #3a3a3c;
    border-left: 2px solid rgba(255,107,61,0.25);
    padding-left: 10px;
    line-height: 1.5;
  }}

  /* ── Requests leaderboard ── */
  .req-list {{
    background: #ffffff;
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
  }}
  .req-row {{
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 16px 24px;
    border-bottom: 1px solid rgba(0,0,0,0.04);
  }}
  .req-row:last-child {{ border-bottom: none; }}
  .req-rank {{
    font-size: 11px;
    color: #8e8e93;
    min-width: 20px;
    font-weight: 500;
  }}
  .req-count {{
    font-size: 24px;
    font-weight: 700;
    color: #ff6b3d;
    min-width: 44px;
    line-height: 1;
    letter-spacing: -0.5px;
  }}
  .req-detail {{ display: flex; flex-direction: column; gap: 3px; }}
  .req-label {{ font-size: 14px; font-weight: 600; color: #1c1c1e; }}
  .req-quote {{ font-size: 12px; color: #8e8e93; font-style: italic; }}
  .empty-state {{ color: #8e8e93; font-size: 14px; padding: 24px; }}

  /* ── Fan Vision ── */
  .vision-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 12px;
  }}
  .vision-card {{
    background: #ffffff;
    border-radius: 16px;
    padding: 20px 22px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .vision-badge {{
    display: inline-flex;
    align-items: center;
    background: rgba(124,92,191,0.08);
    color: #7c5cbf;
    font-size: 11px;
    font-weight: 700;
    border-radius: 8px;
    padding: 4px 10px;
    align-self: flex-start;
    letter-spacing: .02em;
  }}
  .vision-quote {{
    font-size: 13px;
    color: #3a3a3c;
    line-height: 1.55;
  }}
  .vision-author {{
    font-size: 11px;
    color: #8e8e93;
    font-weight: 500;
  }}

  /* ── Quotable Lines ── */
  .ql-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 12px;
  }}
  .ql-card {{
    background: #ffffff;
    border-radius: 16px;
    padding: 20px 22px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
    display: flex;
    flex-direction: column;
    gap: 10px;
    border-left: 3px solid #ff6b3d;
  }}
  .ql-expandable {{
    cursor: pointer;
    transition: box-shadow 0.15s;
  }}
  .ql-expandable:hover {{
    box-shadow: 0 4px 20px rgba(0,0,0,0.10), 0 1px 3px rgba(0,0,0,0.04);
  }}
  .ql-header {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
  }}
  .ql-line {{
    font-size: 15px;
    font-weight: 600;
    color: #1c1c1e;
    line-height: 1.5;
    letter-spacing: -0.1px;
    flex: 1;
  }}
  .ql-chevron {{
    font-size: 18px;
    color: #8e8e93;
    line-height: 1;
    transition: transform 0.2s;
    flex-shrink: 0;
    margin-top: 1px;
  }}
  .ql-card.open .ql-chevron {{ transform: rotate(90deg); }}
  .ql-meta {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .ql-context {{
    font-size: 12px;
    color: #8e8e93;
    line-height: 1.5;
    flex: 1;
  }}
  .ql-count {{
    font-size: 11px;
    font-weight: 600;
    color: #ff6b3d;
    white-space: nowrap;
    background: rgba(255,107,61,0.08);
    padding: 3px 8px;
    border-radius: 6px;
  }}
  .ql-drawer {{
    display: none;
    border-top: 1px solid rgba(0,0,0,0.06);
    padding-top: 14px;
    margin-top: 2px;
  }}
  .ql-card.open .ql-drawer {{ display: block; }}
  .ql-comments {{ display: flex; flex-direction: column; gap: 10px; }}
  .ql-comment {{
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 10px 12px;
    background: #f8f8fa;
    border-radius: 10px;
  }}
  .ql-cmt-author {{
    font-size: 11px;
    font-weight: 600;
    color: #ff6b3d;
  }}
  .ql-cmt-text {{
    font-size: 13px;
    color: #3a3a3c;
    line-height: 1.5;
  }}

  /* ── Top comments ── */
  .top-comments-list {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }}
  .comment-card {{
    background: #ffffff;
    border-radius: 16px;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
    flex: 0 1 300px;
  }}
  .comment-footer {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 2px;
  }}
  .comment-text {{
    font-size: 13px;
    color: #1c1c1e;
    line-height: 1.55;
  }}
  .comment-likes {{
    font-size: 11px;
    color: #8e8e93;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 4px;
  }}
  .like-heart {{ color: #ff6b3d; font-size: 12px; }}
</style>
</head>
<body>

<header class="header">
  <div class="tool-name">
    <svg width="18" height="13" viewBox="0 0 18 13" fill="none" xmlns="http://www.w3.org/2000/svg" style="display:inline-block;vertical-align:middle;margin-right:6px;margin-top:-2px">
      <path d="M17.6 2.03C17.4 1.29 16.83 0.71 16.1 0.5C14.7 0.13 9 0.13 9 0.13C9 0.13 3.3 0.13 1.9 0.5C1.17 0.71 0.6 1.29 0.4 2.03C0 3.44 0 6.38 0 6.38C0 6.38 0 9.32 0.4 10.73C0.6 11.47 1.17 12.03 1.9 12.24C3.3 12.61 9 12.61 9 12.61C9 12.61 14.7 12.61 16.1 12.24C16.83 12.03 17.4 11.47 17.6 10.73C18 9.32 18 6.38 18 6.38C18 6.38 18 3.44 17.6 2.03Z" fill="#FF0000"/>
      <path d="M7.18 9.07L11.88 6.38L7.18 3.69V9.07Z" fill="white"/>
    </svg>
    Fan Signal
  </div>
  <div class="video-title">{html_escape(video_title)}</div>
  <div class="video-meta">{html_escape(channel)} &nbsp;·&nbsp; {total_comments:,} comments analyzed</div>
</header>

<nav class="tab-bar">
  {tab_buttons}
</nav>

<div class="tab-content">
  {tab_panels}
</div>

<script>
document.querySelectorAll('.tab-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('.tab-panel').forEach(function(p) {{ p.classList.remove('active'); }});
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  }});
}});

function toggleQL(drawerId, card) {{
  card.classList.toggle('open');
}}
</script>

</body>
</html>"""


# ── Compare mode (two videos side by side) ───────────────────────────────────

def build_compare_html(reports: list) -> str:
    """Build a side-by-side comparison of two videos' request leaderboards."""
    cols = ""
    for r in reports:
        reqs_html = build_requests_html(r["requests_data"])
        cols += f"""<div class="compare-col">
          <div class="compare-header">
            <div class="video-title">{html_escape(r["title"])}</div>
            <div class="video-meta">{html_escape(r["channel"])} · {r["total_comments"]} comments</div>
          </div>
          <div class="req-list">{reqs_html}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fan Signal — Compare</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #f7f7f8; color: #111118;
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 15px; line-height: 1.6; padding: 0 0 60px;
  }}
  .top-bar {{
    background: #ffffff;
    border-bottom: 1px solid rgba(0,0,0,0.08);
    padding: 24px 40px;
  }}
  .tool-name {{
    font-size: 11px; font-weight: 600; letter-spacing: .12em;
    text-transform: uppercase; color: #1db954; margin-bottom: 4px;
  }}
  .top-bar h1 {{ font-size: 20px; font-weight: 600; color: #111118; }}
  .compare-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    padding: 28px 40px;
  }}
  .compare-header {{ margin-bottom: 16px; }}
  .video-title {{ font-size: 17px; font-weight: 600; margin-bottom: 3px; color: #111118; }}
  .video-meta {{ font-size: 13px; color: #6b6b7a; }}
  .req-list {{
    background: #ffffff;
    border: 1px solid rgba(0,0,0,0.08);
    border-radius: 12px; overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
  }}
  .req-row {{
    display: flex; align-items: flex-start; gap: 16px;
    padding: 16px 22px;
    border-bottom: 1px solid rgba(0,0,0,0.06);
  }}
  .req-row:last-child {{ border-bottom: none; }}
  .req-rank {{ font-size: 11px; color: #6b6b7a; min-width: 24px; padding-top: 2px; }}
  .req-count {{ font-size: 22px; font-weight: 600; color: #22c1a4; min-width: 40px; line-height: 1.1; }}
  .req-detail {{ display: flex; flex-direction: column; gap: 4px; }}
  .req-label {{ font-size: 14px; font-weight: 600; color: #111118; }}
  .req-quote {{ font-size: 12px; color: #6b6b7a; font-style: italic; }}
  .empty-state {{ color: #6b6b7a; font-size: 14px; padding: 20px 22px; }}
</style>
</head>
<body>
<header class="top-bar">
  <div class="tool-name">Fan Signal · Compare</div>
  <h1>Fan Request Leaderboard — Side by Side</h1>
</header>
<div class="compare-grid">{cols}</div>
</body>
</html>"""


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_single(url: str, yt_key: str) -> dict:
    """Run the full pipeline for one URL. Returns data dict for compare mode."""
    video_id = extract_video_id(url)
    if not video_id:
        print(f"❌  Could not extract a video ID from: {url}")
        sys.exit(1)

    print(f"🎵  Video ID: {video_id}")

    # 1. Video metadata
    print("⏳  Fetching video info…")
    try:
        info = get_video_info(video_id, yt_key)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            print("❌  Video not found.")
        else:
            print(f"❌  YouTube API error: {e}")
        sys.exit(1)

    if not info:
        print("❌  Video not found or is private.")
        sys.exit(1)

    title = info["title"]
    channel = info["channel"]
    duration = info["duration_seconds"]

    print(f'   "{title}" by {channel} ({fmt_time(duration)})')

    # 2. Comments
    print(f"⏳  Fetching up to {MAX_COMMENTS} comments…")
    try:
        comments = fetch_comments(video_id, yt_key)
    except requests.HTTPError as e:
        print(f"❌  Error fetching comments: {e}")
        sys.exit(1)

    if not comments:
        print("⚠️  No comments retrieved (disabled or empty).")
        comments = []

    print(f"   Got {len(comments)} comments.")

    # 3. Heatmap (pure Python)
    ts_counter = parse_timestamps(comments, duration)
    distinct_ts = len(ts_counter)
    print(f"   Found {distinct_ts} distinct timestamps in comments.")

    buckets, buckets_comments = build_heatmap_data(ts_counter, comments, duration)
    top_ts = top_timestamps(ts_counter, n=5)

    tier = get_tier(distinct_ts)
    print(f"   Report tier: {tier} ({'full heatmap' if tier == 'A' else 'clip cards only' if tier == 'B' else 'requests only'})")

    # Build comments_at_ts: map from "M:SS" -> up to 3 raw comment texts
    comments_at_ts = {}
    for ts_secs, _cnt in top_ts:
        label = fmt_time(ts_secs)
        matched = [
            c["text"] if isinstance(c, dict) else c
            for c in comments
            if fmt_time_in_comment(ts_secs, c["text"] if isinstance(c, dict) else c)
        ][:3]
        comments_at_ts[label] = matched

    # 4. Claude analysis
    print("🤖  Analyzing with Claude…")
    analysis = analyze_with_claude(comments, top_ts)
    clip_quotes = analysis["clip_quotes"]
    requests_data = analysis["requests"]
    sentiment_pct = analysis["sentiment_positive_pct"]
    fan_vision = analysis["fan_vision"]
    quotable_lines = analysis.get("quotable_lines", [])

    return {
        "title": title,
        "channel": channel,
        "duration": duration,
        "total_comments": len(comments),
        "tier": tier,
        "buckets": buckets,
        "buckets_comments": buckets_comments,
        "top_ts": top_ts,
        "clip_quotes": clip_quotes,
        "requests_data": requests_data,
        "sentiment_pct": sentiment_pct,
        "comments": comments,
        "fan_vision": fan_vision,
        "comments_at_ts": comments_at_ts,
        "quotable_lines": quotable_lines,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fan Signal — YouTube comment analyzer for music artists."
    )
    parser.add_argument("url", nargs="?", help="YouTube URL to analyze")
    parser.add_argument("--compare", nargs=2, metavar=("URL1", "URL2"),
                        help="Compare two videos side by side")
    args = parser.parse_args()

    # Load API keys
    yt_key = os.getenv("YOUTUBE_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if not yt_key:
        print("❌  YOUTUBE_API_KEY not set. Add it to your .env file.")
        sys.exit(1)
    if not anthropic_key:
        print("❌  ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    # ── Compare mode ──
    if args.compare:
        url1, url2 = args.compare
        print(f"\n── Video 1: {url1}")
        data1 = run_single(url1, yt_key)
        print(f"\n── Video 2: {url2}")
        data2 = run_single(url2, yt_key)

        html = build_compare_html([data1, data2])
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fan_signal_compare.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n✅  Compare report saved → {out_path}")
        webbrowser.open(f"file://{out_path}")
        return

    # ── Single mode ──
    if not args.url:
        parser.print_help()
        sys.exit(1)

    data = run_single(args.url, yt_key)

    # 5. Build HTML
    print("📊  Building report…")
    html = build_html_report(
        video_title=data["title"],
        channel=data["channel"],
        duration_seconds=data["duration"],
        total_comments=data["total_comments"],
        tier=data["tier"],
        buckets=data["buckets"],
        top_ts=data["top_ts"],
        clip_quotes=data["clip_quotes"],
        requests_data=data["requests_data"],
        sentiment_pct=data["sentiment_pct"],
        comments=data.get("comments"),
        buckets_comments=data.get("buckets_comments"),
        fan_vision=data.get("fan_vision", []),
        comments_at_ts=data.get("comments_at_ts", {}),
    )

    # 6. Write + open
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fan_signal_report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅  Report saved → {out_path}")
    webbrowser.open(f"file://{out_path}")


if __name__ == "__main__":
    main()
