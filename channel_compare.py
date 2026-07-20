#!/usr/bin/env python3
"""
channel_compare.py — YouTube channel comparison tool.
Compare two YouTube channels side by side: stats, momentum, content, audience sentiment.
"""

import os
import re
import json
import sys
from datetime import datetime, timedelta, timezone
from collections import Counter

import requests
import anthropic
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/Desktop/.env"))
load_dotenv()

# ── Model config ─────────────────────────────────────────────────────────────
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5"

MAX_COMMENTS_PER_CHANNEL = 200
RECENT_VIDEOS_COUNT = 20

# YouTube has no "isShort" flag; treat any video <= this duration as a Short.
SHORT_MAX_SECONDS = 60

# In-memory cache for handle -> channel_id resolution (per process)
_channel_id_cache = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_iso_duration(duration: str) -> int:
    """Return total seconds from ISO-8601 duration string like PT3M12S."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    secs = int(m.group(3) or 0)
    return h * 3600 + mins * 60 + secs


def extract_channel_id(url: str, youtube_service):
    """
    Extract channel ID from various YouTube URL formats.
    Handles: /channel/UCxxxx, /@handle, /c/name, /user/name
    """
    url = url.strip()

    # Direct channel ID: /channel/UCxxxx
    match = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)

    # Custom URL handle: /@handle or /c/name or /user/name
    handle_match = re.search(r"(?:/@|/c/|/user/)([A-Za-z0-9_.-]+)", url)
    if handle_match:
        handle = handle_match.group(1)
        if handle in _channel_id_cache:
            return _channel_id_cache[handle]
        try:
            request = youtube_service.search().list(
                part="snippet",
                q=handle,
                type="channel",
                maxResults=5
            )
            result = request.execute()
            items = result.get("items", [])
            if items:
                # YouTube often returns an auto-generated "ARTIST - Topic" channel
                # first; those are wrong (audio-only). Pick the first non-Topic result.
                chosen = None
                for it in items:
                    title = it["snippet"].get("title", "")
                    if not title.rstrip().lower().endswith("- topic"):
                        chosen = it
                        break
                if chosen is None:
                    chosen = items[0]
                cid = chosen["snippet"]["channelId"]
                _channel_id_cache[handle] = cid
                return cid
        except Exception as e:
            print(f"Error searching for channel handle {handle}: {e}", file=sys.stderr)

    raise ValueError(f"Could not extract channel ID from URL: {url}")


def get_channel_info(channel_id: str, youtube_service):
    """
    Fetch channel metadata: name, handle, description, stats, upload playlist.
    """
    request = youtube_service.channels().list(
        part="snippet,statistics,contentDetails",
        id=channel_id
    )
    result = request.execute()
    items = result.get("items", [])
    if not items:
        raise ValueError(f"Channel not found: {channel_id}")

    channel = items[0]
    snippet = channel["snippet"]
    stats = channel["statistics"]
    content_details = channel["contentDetails"]

    # Handle hidden subscriber counts
    sub_count = stats.get("subscriberCount")
    if sub_count is None or stats.get("hiddenSubscriberCount") == True:
        sub_count = 0
    else:
        sub_count = int(sub_count)

    return {
        "id": channel_id,
        "name": snippet.get("title", ""),
        "handle": snippet.get("customUrl", ""),
        "description": snippet.get("description", "")[:300],
        "thumbnail": snippet["thumbnails"].get("medium", {}).get("url", ""),
        "subscribers": sub_count,
        "total_views": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "created_at": snippet.get("publishedAt", "").split("T")[0],
        "uploads_playlist": content_details["relatedPlaylists"]["uploads"],
    }


def get_recent_videos(uploads_playlist: str, youtube_service, count=20):
    """
    Fetch the last `count` videos from a channel's uploads playlist.
    """
    videos = []

    # Get playlist items
    request = youtube_service.playlistItems().list(
        part="snippet",
        playlistId=uploads_playlist,
        maxResults=min(count, 50)
    )
    result = request.execute()
    video_ids = []
    for item in result.get("items", []):
        video_ids.append(item["snippet"]["resourceId"]["videoId"])

    if not video_ids:
        return videos

    # Get full video details
    request = youtube_service.videos().list(
        part="snippet,statistics,contentDetails",
        id=",".join(video_ids)
    )
    result = request.execute()

    for item in result.get("items", []):
        snippet = item["snippet"]
        stats = item["statistics"]
        duration_str = item["contentDetails"]["duration"]

        videos.append({
            "id": item["id"],
            "title": snippet.get("title", ""),
            "published_at": snippet.get("publishedAt", ""),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "duration_seconds": parse_iso_duration(duration_str),
            "thumbnail": snippet["thumbnails"].get("default", {}).get("url", ""),
        })

    return videos


def get_top_liked_comments(video_ids: list, youtube_service, max_comments=100):
    """
    Fetch comments from multiple videos and return top-liked comments across all.
    Sorts by like count descending.
    """
    comments_with_likes = []

    for video_id in video_ids:
        request = youtube_service.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=100,
            order="relevance",
            textFormat="plainText"
        )

        try:
            while len(comments_with_likes) < max_comments * 2:  # Fetch more than needed to filter by likes
                result = request.execute()
                for item in result.get("items", []):
                    comment_snippet = item["snippet"]["topLevelComment"]["snippet"]
                    comment_text = comment_snippet["textDisplay"]
                    likes = comment_snippet.get("likeCount", 0)
                    comments_with_likes.append({
                        "text": comment_text,
                        "likes": likes
                    })

                # Check for next page
                next_page_token = result.get("nextPageToken")
                if not next_page_token:
                    break
                request = youtube_service.commentThreads().list(
                    part="snippet",
                    videoId=video_id,
                    pageToken=next_page_token,
                    maxResults=100,
                    order="relevance",
                    textFormat="plainText"
                )
        except Exception as e:
            # Comments may be disabled
            pass

    # Sort by likes descending and return just the text
    sorted_comments = sorted(comments_with_likes, key=lambda x: x["likes"], reverse=True)
    return [c["text"] for c in sorted_comments[:max_comments]]


def _parse_published(published_at):
    """Parse an ISO published_at string into an aware UTC datetime, or None."""
    if not published_at:
        return None
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except Exception:
        return None


def filter_last_30_days(videos):
    """Return only videos published within the last 30 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    recent = []
    for v in videos:
        pub = _parse_published(v.get("published_at", ""))
        if pub and pub >= cutoff:
            recent.append(v)
    return recent


def get_top_video_ids_alltime(channel_id, youtube_service, count=5):
    """Return a list of the channel's top all-time video IDs ordered by view count."""
    try:
        request = youtube_service.search().list(
            part="snippet",
            channelId=channel_id,
            order="viewCount",
            type="video",
            maxResults=count,
        )
        result = request.execute()
        return [item["id"]["videoId"] for item in result.get("items", [])
                if item.get("id", {}).get("videoId")]
    except Exception as e:
        print(f"Error fetching top video IDs for {channel_id}: {e}", file=sys.stderr)
        return []


def get_top_videos_from_ids(video_ids, youtube_service):
    """
    Given a list of video IDs (already ordered by view count), fetch stats and
    return list of dicts: title, views, likes, video_id, url, thumbnail.
    Preserves the input ordering. Costs 1 videos().list unit.
    """
    if not video_ids:
        return []

    try:
        request = youtube_service.videos().list(
            part="snippet,statistics",
            id=",".join(video_ids),
        )
        result = request.execute()
    except Exception as e:
        print(f"Error fetching top video stats: {e}", file=sys.stderr)
        return []

    by_id = {item["id"]: item for item in result.get("items", [])}
    videos = []
    for vid in video_ids:
        item = by_id.get(vid)
        if not item:
            continue
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        videos.append({
            "title": snippet.get("title", ""),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
        })
    # videos().list does NOT preserve the order of the IDs passed; re-sort by views.
    videos.sort(key=lambda x: x["views"], reverse=True)
    return videos


def get_top_videos_alltime(channel_id, youtube_service, count=5):
    """
    Return the channel's TRUE all-time top videos by view count.
    List of dicts: title, views, likes, video_id, url, thumbnail.
    """
    video_ids = get_top_video_ids_alltime(channel_id, youtube_service, count)
    if not video_ids:
        return []

    try:
        request = youtube_service.videos().list(
            part="snippet,statistics",
            id=",".join(video_ids),
        )
        result = request.execute()
    except Exception as e:
        print(f"Error fetching top video stats for {channel_id}: {e}", file=sys.stderr)
        return []

    # Preserve search (viewCount) ordering
    by_id = {item["id"]: item for item in result.get("items", [])}
    videos = []
    for vid in video_ids:
        item = by_id.get(vid)
        if not item:
            continue
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        videos.append({
            "title": snippet.get("title", ""),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
        })
    # videos().list does NOT preserve the order of the IDs passed; re-sort by views.
    videos.sort(key=lambda x: x["views"], reverse=True)
    return videos


def get_channel_top_comments(top_video_ids, youtube_service, top_n=20):
    """
    Fetch up to 100 relevance-ordered comments from each of the given (top) videos,
    dedupe by text, sort by likeCount descending, return top_n dicts {text, likes}.
    """
    seen = {}
    for video_id in top_video_ids:
        try:
            request = youtube_service.commentThreads().list(
                part="snippet",
                videoId=video_id,
                maxResults=100,
                order="relevance",
                textFormat="plainText",
            )
            result = request.execute()
        except Exception:
            # Comments disabled or unavailable
            continue

        for item in result.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            text = snippet.get("textDisplay", "").strip()
            if not text:
                continue
            likes = int(snippet.get("likeCount", 0))
            # Keep the highest like count seen for duplicate text, and remember
            # which video that top-liked instance came from.
            if text not in seen or likes > seen[text]["likes"]:
                seen[text] = {"likes": likes, "video_id": video_id}

    comments = [
        {
            "text": t,
            "likes": info["likes"],
            "video_id": info["video_id"],
            "url": f"https://www.youtube.com/watch?v={info['video_id']}",
        }
        for t, info in seen.items()
    ]
    comments.sort(key=lambda c: c["likes"], reverse=True)
    return comments[:top_n]


def calc_velocity_videos(recent_videos, top_n=5):
    """
    Rank recent videos (published in last 90 days) by views per day since posting.
    Returns top_n dicts: title, views, views_per_day (int), days_old (int), url.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=90)
    scored = []
    for v in recent_videos:
        pub = _parse_published(v.get("published_at", ""))
        if not pub or pub < cutoff:
            continue
        # Long-form only: skip Shorts.
        if v.get("duration_seconds", 0) <= SHORT_MAX_SECONDS:
            continue
        days_old = (now - pub).days
        vpd = v["views"] / max(days_old, 1)
        scored.append({
            "title": v.get("title", ""),
            "views": v["views"],
            "views_per_day": int(vpd),
            "days_old": int(days_old),
            "url": f"https://www.youtube.com/watch?v={v['id']}",
        })
    scored.sort(key=lambda x: x["views_per_day"], reverse=True)
    return scored[:top_n]


def calculate_channel_metrics(channel_info, videos):
    """
    Calculate engagement metrics over STRICT last-30-day videos.
    """
    recent = filter_last_30_days(videos)
    # Exclude Shorts: only long-form uploads count toward performance metrics.
    recent = [v for v in recent if v.get("duration_seconds", 0) > SHORT_MAX_SECONDS]

    if not recent:
        return {
            "no_recent": True,
            "avg_views": 0,
            "avg_likes": 0,
            "avg_comments": 0,
            "engagement_rate": 0.0,
            "uploads_last_30": 0,
            "avg_duration_seconds": 0,
        }

    n = len(recent)
    avg_views = sum(v["views"] for v in recent) // n
    avg_likes = sum(v["likes"] for v in recent) // n
    avg_comments = sum(v["comments"] for v in recent) // n
    avg_duration = sum(v["duration_seconds"] for v in recent) // n

    engagement_rate = 0.0
    if avg_views > 0:
        engagement_rate = round(((avg_likes + avg_comments) / avg_views) * 100, 2)

    return {
        "no_recent": False,
        "avg_views": avg_views,
        "avg_likes": avg_likes,
        "avg_comments": avg_comments,
        "engagement_rate": engagement_rate,
        "uploads_last_30": n,
        "avg_duration_seconds": avg_duration,
    }


def analyze_with_claude(channel_a_info, channel_a_videos, channel_a_comments,
                        channel_b_info, channel_b_videos, channel_b_comments):
    """
    Use Claude to analyze both channels and generate comparison insights.
    Returns JSON dict with content strategy, sentiment, audience vibe, and gap analysis.
    """
    # Prepare video titles
    a_titles = [v["title"] for v in channel_a_videos[:15]]
    b_titles = [v["title"] for v in channel_b_videos[:15]]

    # Prepare comment samples (top-liked comments as {text, likes} dicts)
    def comments_to_text(comments):
        if not comments:
            return "No comments available"
        return "\n".join(f"({c['likes']} likes) {c['text']}" for c in comments[:100])

    a_comments_text = comments_to_text(channel_a_comments)
    b_comments_text = comments_to_text(channel_b_comments)

    prompt = f"""Analyze these two YouTube channels and provide a comparison.

Channel A: {channel_a_info['name']}
Subscribers: {channel_a_info['subscribers']}
Total Views: {channel_a_info['total_views']}
Recent Video Titles:
{json.dumps(a_titles, indent=2)}

Sample Comments:
{a_comments_text}

---

Channel B: {channel_b_info['name']}
Subscribers: {channel_b_info['subscribers']}
Total Views: {channel_b_info['total_views']}
Recent Video Titles:
{json.dumps(b_titles, indent=2)}

Sample Comments:
{b_comments_text}

---

Provide ONLY valid JSON (no markdown, no extra text) in this exact structure:
{{
  "channel_a": {{
    "content_strategy": "one sentence describing their content approach",
    "sentiment_score": 85,
    "sentiment_label": "Very Positive",
    "audience_vibe": "one sentence on what fans say/feel",
    "top_themes": ["theme1", "theme2", "theme3"]
  }},
  "channel_b": {{
    "content_strategy": "one sentence describing their content approach",
    "sentiment_score": 72,
    "sentiment_label": "Positive",
    "audience_vibe": "one sentence on what fans say/feel",
    "top_themes": ["theme1", "theme2", "theme3"]
  }},
  "gap_analysis": {{
    "a_winning": "one sentence where channel A outperforms",
    "b_winning": "one sentence where channel B outperforms",
    "a_opportunity": "one sentence biggest growth opportunity for channel A",
    "b_opportunity": "one sentence biggest growth opportunity for channel B"
  }}
}}

Sentiment labels must be one of: "Very Positive", "Positive", "Mixed", "Negative"
Sentiment scores must be 0-100.
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    response_text = message.content[0].text.strip()

    # Try to extract JSON if wrapped in markdown
    if response_text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL)
        if match:
            response_text = match.group(1)

    data = json.loads(response_text)
    return data


def build_html_report(channel_a_info, channel_a_metrics, channel_b_info,
                      channel_b_metrics, claude_data, channel_a_comments=None,
                      channel_b_comments=None, channel_a_top_videos=None,
                      channel_b_top_videos=None, channel_a_velocity=None,
                      channel_b_velocity=None):
    """
    Generate a beautiful side-by-side comparison report.
    """

    # Helper: format large numbers
    def fmt_num(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    # Helper: format seconds to mm:ss
    def fmt_duration(secs):
        mins = secs // 60
        secs = secs % 60
        return f"{mins}m {secs}s"

    # Helper: sentiment color
    def sentiment_color(label):
        colors = {
            "Very Positive": "#34C759",
            "Positive": "#88D273",
            "Mixed": "#FF9500",
            "Negative": "#D93025"
        }
        return colors.get(label, "#8E8E93")

    # Helper: honest engagement label
    def engagement_rank(engagement_rate):
        if engagement_rate >= 5.0:
            return "High"
        elif engagement_rate >= 2.0:
            return "Medium"
        else:
            return "Low"

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    # Helper: growth stage
    def growth_stage(subs):
        if subs < 50000:
            return "Emerging"
        elif subs < 500000:
            return "Growing"
        else:
            return "Established"

    channel_a_top_videos = channel_a_top_videos or []
    channel_b_top_videos = channel_b_top_videos or []
    channel_a_velocity = channel_a_velocity or []
    channel_b_velocity = channel_b_velocity or []
    channel_a_comments = channel_a_comments or []
    channel_b_comments = channel_b_comments or []

    # Helper: render a metric row for the last-30-day comparison table
    def metric_row(label, a_val, b_val, a_num, b_num):
        if channel_a_metrics.get("no_recent") and channel_b_metrics.get("no_recent"):
            winner = "—"
        elif channel_a_metrics.get("no_recent"):
            winner = f'<span style="color:#2f76dd;font-weight:700;">{esc(channel_b_info["name"])} 🏆</span>'
        elif channel_b_metrics.get("no_recent"):
            winner = f'<span style="color:#2f76dd;font-weight:700;">{esc(channel_a_info["name"])} 🏆</span>'
        elif a_num > b_num:
            winner = f'<span style="color:#2f76dd;font-weight:700;">{esc(channel_a_info["name"])} 🏆</span>'
        elif b_num > a_num:
            winner = f'<span style="color:#2f76dd;font-weight:700;">{esc(channel_b_info["name"])} 🏆</span>'
        else:
            winner = "—"
        a_disp = "No long-form uploads in the last 30 days" if channel_a_metrics.get("no_recent") else a_val
        b_disp = "No long-form uploads in the last 30 days" if channel_b_metrics.get("no_recent") else b_val
        return (f"<tr><td>{label}</td><td>{a_disp}</td><td>{b_disp}</td>"
                f"<td>{winner}</td></tr>")

    # Helper: render top-videos list for one channel
    def top_videos_html(vids):
        if not vids:
            return '<p style="color: #8e8e93;">No videos available</p>'
        rows = []
        for v in vids[:5]:
            rows.append(
                f'<a href="{esc(v["url"])}" target="_blank" rel="noopener" '
                f'style="text-decoration:none;color:inherit;">'
                f'<div class="top-video-card">'
                f'<div class="top-video-title">{esc(v["title"])}</div>'
                f'<div class="top-video-stats">{fmt_num(v["views"])} views • '
                f'{fmt_num(v["likes"])} likes</div></div></a>'
            )
        return "".join(rows)

    # Helper: render velocity list for one channel
    def velocity_html(vids):
        if not vids:
            return '<p style="color: #8e8e93;">No recent videos in the last 90 days</p>'
        rows = []
        for v in vids[:5]:
            rows.append(
                f'<a href="{esc(v["url"])}" target="_blank" rel="noopener" '
                f'style="text-decoration:none;color:inherit;">'
                f'<div class="top-video-card">'
                f'<div class="top-video-title">{esc(v["title"])}</div>'
                f'<div class="top-video-stats">{v["views_per_day"]:,} views/day • '
                f'{v["days_old"]} days old</div></div></a>'
            )
        return "".join(rows)

    # Helper: render top-liked comments list for one channel
    def top_comments_html(comments):
        if not comments:
            return '<p style="color: #8e8e93; margin-top: 12px;">No comments available</p>'
        rows = ['<p style="font-size: 13px; color: #8e8e93; margin-bottom: 8px; margin-top: 12px;">Top 20 Most-Liked Comments:</p>']
        for c in comments[:20]:
            url = c.get("url") or (
                f'https://www.youtube.com/watch?v={c["video_id"]}'
                if c.get("video_id") else ""
            )
            text_html = esc(c["text"])
            if url:
                view_link = (
                    f'<a href="{esc(url)}" target="_blank" rel="noopener" '
                    f'class="comment-source-link">↗ view</a>'
                )
            else:
                view_link = ""
            rows.append(
                f'<div class="comment-row">'
                f'<span class="like-badge">♥ {c["likes"]:,}</span>'
                f'<span class="comment-text">{text_html}{view_link}</span></div>'
            )
        return "".join(rows)

    a_claude = claude_data.get("channel_a", {})
    b_claude = claude_data.get("channel_b", {})
    gap = claude_data.get("gap_analysis", {})

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Channel Comparison</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f9f9fb;
      min-height: 100vh;
      padding: 24px;
    }}

    .headline {{
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }}

    .hey-mike-footer {{
      position: fixed;
      bottom: 16px;
      right: 16px;
      z-index: 9999;
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 33px;
      color: #2f76dd;
      background: rgba(255,255,255,0.9);
      padding: 10px 18px;
      border-radius: 999px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }}

    .container {{
      max-width: 1200px;
      margin: 0 auto;
    }}

    .header {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
    }}

    .header svg {{
      width: 32px;
      height: 32px;
    }}

    .header h1 {{
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 32px;
      font-weight: 400;
      color: #1c1c1e;
    }}

    .subtitle {{
      font-size: 16px;
      color: #6c6c70;
      margin-bottom: 24px;
    }}

    .back-link {{
      display: inline-block;
      margin-bottom: 16px;
      color: #2f76dd;
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
    }}

    .back-link:hover {{
      text-decoration: underline;
    }}

    .tab-bar {{
      display: flex;
      gap: 8px;
      background: rgba(255,255,255,0.7);
      backdrop-filter: blur(20px);
      border-radius: 12px;
      padding: 6px;
      margin-bottom: 24px;
      position: sticky;
      top: 0;
      z-index: 100;
    }}

    .tab-button {{
      flex: 1;
      padding: 12px 16px;
      border: none;
      border-radius: 8px;
      background: transparent;
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 15px;
      font-weight: 400;
      color: #6c6c70;
      cursor: pointer;
      transition: all 0.15s;
    }}

    .tab-button.active {{
      background: #fff;
      color: #2f76dd;
      box-shadow: 0 2px 8px rgba(47,118,221,0.15);
      border-bottom: 2px solid #2f76dd;
    }}

    .tab-content {{
      display: none;
    }}

    .tab-content.active {{
      display: block;
    }}

    .two-column {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      margin-bottom: 24px;
    }}

    .card {{
      background: #fff;
      border-radius: 16px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.06);
      padding: 24px;
    }}

    .channel-header {{
      display: flex;
      gap: 16px;
      margin-bottom: 20px;
    }}

    .channel-thumb {{
      width: 80px;
      height: 80px;
      border-radius: 50%;
      background: #f2f2f7;
      flex-shrink: 0;
      object-fit: cover;
    }}

    .channel-info h2 {{
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 18px;
      font-weight: 400;
      color: #1c1c1e;
      margin-bottom: 2px;
    }}

    .channel-handle {{
      font-size: 14px;
      color: #8e8e93;
      margin-bottom: 8px;
    }}

    .stats-row {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }}

    .stat-pill {{
      background: #f2f2f7;
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 13px;
      color: #1c1c1e;
      font-weight: 500;
    }}

    .stat-pill .label {{
      color: #8e8e93;
      font-weight: 400;
    }}

    .trend-badge {{
      display: inline-block;
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 500;
    }}

    .winner-badge {{
      background: #e6eefc;
      color: #2f76dd;
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      margin-bottom: 12px;
    }}

    .comparison-table {{
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 24px;
    }}

    .comparison-table th {{
      background: #f2f2f7;
      padding: 12px;
      text-align: left;
      font-weight: 600;
      font-size: 13px;
      color: #1c1c1e;
      border-bottom: 1px solid #e5e5ea;
    }}

    .comparison-table td {{
      padding: 12px;
      border-bottom: 1px solid #e5e5ea;
      font-size: 14px;
    }}

    .comparison-table td:last-child {{
      text-align: center;
      font-weight: 600;
    }}

    .top-video-card {{
      background: #f2f2f7;
      border-radius: 12px;
      padding: 16px;
      margin-top: 12px;
    }}

    .top-video-title {{
      font-weight: 600;
      font-size: 13px;
      color: #1c1c1e;
      margin-bottom: 6px;
    }}

    .top-video-stats {{
      font-size: 12px;
      color: #8e8e93;
    }}

    .sentiment-score {{
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 36px;
      font-weight: 400;
      margin: 12px 0;
    }}

    .sentiment-label {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      color: #fff;
      margin-bottom: 12px;
    }}

    .theme-tag {{
      display: inline-block;
      background: #f2f2f7;
      color: #1c1c1e;
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 12px;
      margin-right: 6px;
      margin-bottom: 6px;
    }}

    .comments-sample {{
      margin-top: 12px;
    }}

    .comment {{
      background: #f2f2f7;
      padding: 10px;
      border-radius: 8px;
      margin-bottom: 8px;
      font-size: 13px;
      color: #1c1c1e;
      line-height: 1.5;
    }}

    .comment-row {{
      display: flex;
      align-items: flex-start;
      gap: 10px;
      background: #f2f2f7;
      padding: 10px;
      border-radius: 8px;
      margin-bottom: 8px;
    }}

    .like-badge {{
      flex-shrink: 0;
      background: #e6eefc;
      color: #2f76dd;
      font-size: 12px;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 999px;
      white-space: nowrap;
    }}

    .comment-text {{
      font-size: 13px;
      color: #1c1c1e;
      line-height: 1.5;
    }}

    .comment-source-link {{
      margin-left: 8px;
      font-size: 12px;
      font-weight: 500;
      color: #2f76dd;
      text-decoration: none;
      white-space: nowrap;
    }}

    .comment-source-link:hover {{
      text-decoration: underline;
    }}

    /* ── Performance sub-tabs ─────────────────────────────────── */
    .subtab-bar {{
      display: flex;
      gap: 6px;
      background: #f2f2f7;
      border-radius: 10px;
      padding: 4px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }}

    .subtab {{
      padding: 8px 14px;
      border: none;
      border-radius: 7px;
      background: transparent;
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 13px;
      font-weight: 400;
      color: #8e8e93;
      cursor: pointer;
      transition: all 0.15s;
    }}

    .subtab.active {{
      background: #2f76dd;
      color: #fff;
      box-shadow: 0 1px 4px rgba(47,118,221,0.25);
    }}

    .subtab-panel {{
      display: none;
    }}

    .subtab-panel.active {{
      display: block;
    }}

    .section-subhead {{
      font-size: 14px;
      color: #6c6c70;
      margin-bottom: 16px;
    }}

    .gap-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 24px;
    }}

    .gap-card {{
      background: #f2f2f7;
      border-radius: 12px;
      padding: 20px;
    }}

    .gap-card h3 {{
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 14px;
      font-weight: 400;
      color: #1c1c1e;
      margin-bottom: 8px;
    }}

    .gap-card p {{
      font-size: 13px;
      color: #3c3c43;
      line-height: 1.6;
    }}

    .takeaway-card {{
      background: #fff;
      border-radius: 16px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.06);
      padding: 24px;
      text-align: center;
      margin-bottom: 24px;
    }}

    .takeaway-card h3 {{
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 14px;
      font-weight: 400;
      color: #8e8e93;
      margin-bottom: 8px;
    }}

    .takeaway-card p {{
      font-size: 16px;
      font-weight: 500;
      color: #1c1c1e;
      line-height: 1.6;
    }}

    @media (max-width: 768px) {{
      .two-column {{
        grid-template-columns: 1fr;
      }}

      .gap-grid {{
        grid-template-columns: 1fr;
      }}

      .tab-bar {{
        overflow-x: auto;
      }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <a href="/" class="back-link">← Compare another pair</a>

    <div class="header">
      <svg viewBox="0 0 18 13" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M17.6 2.03C17.4 1.29 16.83 0.71 16.1 0.5C14.7 0.13 9 0.13 9 0.13C9 0.13 3.3 0.13 1.9 0.5C1.17 0.71 0.6 1.29 0.4 2.03C0 3.44 0 6.38 0 6.38C0 6.38 0 9.32 0.4 10.73C0.6 11.47 1.17 12.03 1.9 12.24C3.3 12.61 9 12.61 9 12.61C9 12.61 14.7 12.61 16.1 12.24C16.83 12.03 17.4 11.47 17.6 10.73C18 9.32 18 6.38 18 6.38C18 6.38 18 3.44 17.6 2.03Z" fill="#2F76DD"/>
        <path d="M7.18 9.07L11.88 6.38L7.18 3.69V9.07Z" fill="white"/>
      </svg>
      <h1>Channel Comparison</h1>
    </div>
    <p class="subtitle">{channel_a_info['name']} vs {channel_b_info['name']}</p>

    <div class="tab-bar">
      <button class="tab-button active" onclick="switchTab(event, 'overview')">Overview</button>
      <button class="tab-button" onclick="switchTab(event, 'performance')">Performance</button>
      <button class="tab-button" onclick="switchTab(event, 'audience')">Audience</button>
      <button class="tab-button" onclick="switchTab(event, 'gap')">Gap Analysis</button>
    </div>

    <!-- OVERVIEW TAB -->
    <div id="overview" class="tab-content active">
      <div class="two-column">
        <!-- Channel A -->
        <div class="card">
          <div class="channel-header">
            <img src="{channel_a_info['thumbnail']}" alt="{channel_a_info['name']}" class="channel-thumb" />
            <div class="channel-info">
              <h2>{channel_a_info['name']}</h2>
              <div class="channel-handle">@{channel_a_info['handle'] or 'channel'}</div>
            </div>
          </div>

          <p style="font-size: 13px; color: #6c6c70; margin-bottom: 12px; line-height: 1.5;">
            {channel_a_info['description']}
          </p>

          <div class="stats-row">
            <div class="stat-pill"><span class="label">Subscribers</span><br/>{fmt_num(channel_a_info['subscribers'])}</div>
            <div class="stat-pill"><span class="label">Total Views</span><br/>{fmt_num(channel_a_info['total_views'])}</div>
            <div class="stat-pill"><span class="label">Videos</span><br/>{channel_a_info['video_count']}</div>
            <div class="stat-pill"><span class="label">Since</span><br/>{channel_a_info['created_at'][:4]}</div>
          </div>

          <div class="stats-row">
            <div class="stat-pill"><span class="label">Engagement</span><br/>{engagement_rank(channel_a_metrics['engagement_rate'])}</div>
            <div class="stat-pill"><span class="label">Growth Stage</span><br/>{growth_stage(channel_a_info['subscribers'])}</div>
          </div>
        </div>

        <!-- Channel B -->
        <div class="card">
          <div class="channel-header">
            <img src="{channel_b_info['thumbnail']}" alt="{channel_b_info['name']}" class="channel-thumb" />
            <div class="channel-info">
              <h2>{channel_b_info['name']}</h2>
              <div class="channel-handle">@{channel_b_info['handle'] or 'channel'}</div>
            </div>
          </div>

          <p style="font-size: 13px; color: #6c6c70; margin-bottom: 12px; line-height: 1.5;">
            {channel_b_info['description']}
          </p>

          <div class="stats-row">
            <div class="stat-pill"><span class="label">Subscribers</span><br/>{fmt_num(channel_b_info['subscribers'])}</div>
            <div class="stat-pill"><span class="label">Total Views</span><br/>{fmt_num(channel_b_info['total_views'])}</div>
            <div class="stat-pill"><span class="label">Videos</span><br/>{channel_b_info['video_count']}</div>
            <div class="stat-pill"><span class="label">Since</span><br/>{channel_b_info['created_at'][:4]}</div>
          </div>

          <div class="stats-row">
            <div class="stat-pill"><span class="label">Engagement</span><br/>{engagement_rank(channel_b_metrics['engagement_rate'])}</div>
            <div class="stat-pill"><span class="label">Growth Stage</span><br/>{growth_stage(channel_b_info['subscribers'])}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- PERFORMANCE TAB -->
    <div id="performance" class="tab-content">
      <div class="subtab-bar">
        <button class="subtab active" onclick="showSubTab('subtab-general', this)">General</button>
        <button class="subtab" onclick="showSubTab('subtab-topvideos', this)">Top Videos (All-Time)</button>
        <button class="subtab" onclick="showSubTab('subtab-velocity', this)">Fastest Out the Gate</button>
      </div>

      <!-- SUB-TAB: General -->
      <div id="subtab-general" class="subtab-panel active">
        <div class="card">
          <p class="section-subhead">Metrics reflect long-form uploads (over 60s) from the last 30 days. Shorts are excluded.</p>
          <table class="comparison-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th>{esc(channel_a_info['name'])}</th>
                <th>{esc(channel_b_info['name'])}</th>
                <th>Winner</th>
              </tr>
            </thead>
            <tbody>
              {metric_row("Avg Views per Video", fmt_num(channel_a_metrics['avg_views']), fmt_num(channel_b_metrics['avg_views']), channel_a_metrics['avg_views'], channel_b_metrics['avg_views'])}
              {metric_row("Avg Likes per Video", fmt_num(channel_a_metrics['avg_likes']), fmt_num(channel_b_metrics['avg_likes']), channel_a_metrics['avg_likes'], channel_b_metrics['avg_likes'])}
              {metric_row("Engagement Rate", f"{channel_a_metrics['engagement_rate']:.2f}%", f"{channel_b_metrics['engagement_rate']:.2f}%", channel_a_metrics['engagement_rate'], channel_b_metrics['engagement_rate'])}
              {metric_row("Long-Form Videos (last 30 days)", f"{channel_a_metrics['uploads_last_30']} long-form videos in last 30 days", f"{channel_b_metrics['uploads_last_30']} long-form videos in last 30 days", channel_a_metrics['uploads_last_30'], channel_b_metrics['uploads_last_30'])}
              {metric_row("Avg Video Length", fmt_duration(channel_a_metrics['avg_duration_seconds']), fmt_duration(channel_b_metrics['avg_duration_seconds']), channel_a_metrics['avg_duration_seconds'], channel_b_metrics['avg_duration_seconds'])}
            </tbody>
          </table>
        </div>
      </div>

      <!-- SUB-TAB: Top Videos (All-Time) -->
      <div id="subtab-topvideos" class="subtab-panel">
        <div class="card">
          <h3 class="headline" style="font-size: 16px; font-weight: 400; margin-bottom: 12px;">Top Videos (all-time, by views)</h3>
          <div class="two-column">
            <div>
              <h4 style="font-size:14px;font-weight:600;margin-bottom:8px;">{esc(channel_a_info['name'])}</h4>
              {top_videos_html(channel_a_top_videos)}
            </div>
            <div>
              <h4 style="font-size:14px;font-weight:600;margin-bottom:8px;">{esc(channel_b_info['name'])}</h4>
              {top_videos_html(channel_b_top_videos)}
            </div>
          </div>
        </div>
      </div>

      <!-- SUB-TAB: Fastest Out the Gate -->
      <div id="subtab-velocity" class="subtab-panel">
        <div class="card">
          <h3 class="headline" style="font-size: 16px; font-weight: 400; margin-bottom: 4px;">Fastest Out the Gate</h3>
          <p class="section-subhead">Long-form videos (over 60s) from the last 90 days, ranked by views per day since posting.</p>
          <div class="two-column">
            <div>
              <h4 style="font-size:14px;font-weight:600;margin-bottom:8px;">{esc(channel_a_info['name'])}</h4>
              {velocity_html(channel_a_velocity)}
            </div>
            <div>
              <h4 style="font-size:14px;font-weight:600;margin-bottom:8px;">{esc(channel_b_info['name'])}</h4>
              {velocity_html(channel_b_velocity)}
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- AUDIENCE TAB -->
    <div id="audience" class="tab-content">
      <div class="two-column">
        <div class="card">
          <h3 class="headline" style="font-size: 18px; font-weight: 400; margin-bottom: 12px;">{channel_a_info['name']}</h3>

          <div class="sentiment-label" style="background-color: {sentiment_color(a_claude.get('sentiment_label', 'Mixed'))};">
            {a_claude.get('sentiment_label', 'Unknown')}
          </div>

          <div class="sentiment-score" style="color: {sentiment_color(a_claude.get('sentiment_label', 'Mixed'))};">
            {a_claude.get('sentiment_score', '—')}
          </div>

          <p style="font-size: 13px; color: #3c3c43; margin-bottom: 12px; line-height: 1.6;">
            <strong>Strategy:</strong> {a_claude.get('content_strategy', 'N/A')}
          </p>

          <p style="font-size: 13px; color: #3c3c43; margin-bottom: 12px; line-height: 1.6;">
            <strong>Vibe:</strong> {a_claude.get('audience_vibe', 'N/A')}
          </p>

          <p style="font-size: 13px; color: #8e8e93; margin-bottom: 8px;">Top Themes:</p>
          <div>
            {' '.join([f'<div class="theme-tag">{theme}</div>' for theme in a_claude.get('top_themes', [])])}
          </div>

          <div class="comments-sample">{top_comments_html(channel_a_comments)}</div>
        </div>

        <div class="card">
          <h3 class="headline" style="font-size: 18px; font-weight: 400; margin-bottom: 12px;">{channel_b_info['name']}</h3>

          <div class="sentiment-label" style="background-color: {sentiment_color(b_claude.get('sentiment_label', 'Mixed'))};">
            {b_claude.get('sentiment_label', 'Unknown')}
          </div>

          <div class="sentiment-score" style="color: {sentiment_color(b_claude.get('sentiment_label', 'Mixed'))};">
            {b_claude.get('sentiment_score', '—')}
          </div>

          <p style="font-size: 13px; color: #3c3c43; margin-bottom: 12px; line-height: 1.6;">
            <strong>Strategy:</strong> {b_claude.get('content_strategy', 'N/A')}
          </p>

          <p style="font-size: 13px; color: #3c3c43; margin-bottom: 12px; line-height: 1.6;">
            <strong>Vibe:</strong> {b_claude.get('audience_vibe', 'N/A')}
          </p>

          <p style="font-size: 13px; color: #8e8e93; margin-bottom: 8px;">Top Themes:</p>
          <div>
            {' '.join([f'<div class="theme-tag">{theme}</div>' for theme in b_claude.get('top_themes', [])])}
          </div>

          <div class="comments-sample">{top_comments_html(channel_b_comments)}</div>
        </div>
      </div>
    </div>

    <!-- GAP ANALYSIS TAB -->
    <div id="gap" class="tab-content">
      <div class="gap-grid">
        <div class="gap-card">
          <h3>🏆 {channel_a_info['name']} is Winning</h3>
          <p>{gap.get('a_winning', 'N/A')}</p>
        </div>
        <div class="gap-card">
          <h3>🏆 {channel_b_info['name']} is Winning</h3>
          <p>{gap.get('b_winning', 'N/A')}</p>
        </div>
        <div class="gap-card">
          <h3>💡 {channel_a_info['name']}'s Opportunity</h3>
          <p>{gap.get('a_opportunity', 'N/A')}</p>
        </div>
        <div class="gap-card">
          <h3>💡 {channel_b_info['name']}'s Opportunity</h3>
          <p>{gap.get('b_opportunity', 'N/A')}</p>
        </div>
      </div>

      <div class="takeaway-card">
        <h3>Key Takeaway</h3>
        <p>{channel_a_info['name']} focuses on {a_claude.get('content_strategy', 'content').lower()}, while {channel_b_info['name']} pursues {b_claude.get('content_strategy', 'content').lower()}. Both channels should focus on deepening audience engagement through their unique strengths.</p>
      </div>
    </div>
  </div>

  <script>
    function switchTab(event, tabName) {{
      // Hide all tabs
      var contents = document.querySelectorAll('.tab-content');
      contents.forEach(c => c.classList.remove('active'));

      // Remove active from all buttons
      var buttons = document.querySelectorAll('.tab-button');
      buttons.forEach(b => b.classList.remove('active'));

      // Show selected tab
      document.getElementById(tabName).classList.add('active');
      event.target.classList.add('active');
    }}

    function showSubTab(panelId, el) {{
      // Scope to the Performance tab so this never touches the main tabs.
      var perf = document.getElementById('performance');
      if (!perf) return;
      perf.querySelectorAll('.subtab-panel').forEach(p => p.classList.remove('active'));
      perf.querySelectorAll('.subtab').forEach(b => b.classList.remove('active'));
      var panel = document.getElementById(panelId);
      if (panel) panel.classList.add('active');
      if (el) el.classList.add('active');
    }}
  </script>

  <div class="hey-mike-footer">Powered by Hey Mike</div>
</body>
</html>"""

    return html


def run_comparison(url_a: str, url_b: str):
    """
    Full pipeline: extract channel IDs, fetch data, calculate metrics, analyze with Claude,
    build HTML report.
    """
    # Build YouTube service
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

    # Extract channel IDs
    print("Extracting channel IDs...", file=sys.stderr)
    channel_a_id = extract_channel_id(url_a, youtube)
    channel_b_id = extract_channel_id(url_b, youtube)

    # Fetch channel info
    print("Fetching channel data...", file=sys.stderr)
    channel_a_info = get_channel_info(channel_a_id, youtube)
    channel_b_info = get_channel_info(channel_b_id, youtube)

    # Fetch recent videos
    print("Analyzing recent videos...", file=sys.stderr)
    channel_a_videos = get_recent_videos(channel_a_info["uploads_playlist"], youtube, RECENT_VIDEOS_COUNT)
    channel_b_videos = get_recent_videos(channel_b_info["uploads_playlist"], youtube, RECENT_VIDEOS_COUNT)

    # Fetch TRUE all-time top videos with a SINGLE search per channel (top 10 IDs).
    # Top 10 IDs feed comment mining; the first 5 (+ stats) are shown as Top Videos.
    print("Fetching all-time top videos...", file=sys.stderr)
    channel_a_top10_ids = get_top_video_ids_alltime(channel_a_id, youtube, 10)
    channel_b_top10_ids = get_top_video_ids_alltime(channel_b_id, youtube, 10)
    channel_a_top_videos = get_top_videos_from_ids(channel_a_top10_ids[:5], youtube)
    channel_b_top_videos = get_top_videos_from_ids(channel_b_top10_ids[:5], youtube)

    # Fetch top-liked comments from each channel's top all-time videos
    print("Reading audience comments...", file=sys.stderr)
    channel_a_comments = get_channel_top_comments(channel_a_top10_ids, youtube, 20)
    channel_b_comments = get_channel_top_comments(channel_b_top10_ids, youtube, 20)

    # Calculate metrics (strict last 30 days)
    print("Calculating metrics...", file=sys.stderr)
    channel_a_metrics = calculate_channel_metrics(channel_a_info, channel_a_videos)
    channel_b_metrics = calculate_channel_metrics(channel_b_info, channel_b_videos)

    # Velocity: fastest recent videos (last 90 days)
    channel_a_velocity = calc_velocity_videos(channel_a_videos, 5)
    channel_b_velocity = calc_velocity_videos(channel_b_videos, 5)

    # Analyze with Claude
    print("Running AI analysis...", file=sys.stderr)
    claude_data = analyze_with_claude(channel_a_info, channel_a_videos, channel_a_comments,
                                      channel_b_info, channel_b_videos, channel_b_comments)

    # Build HTML report
    print("Building report...", file=sys.stderr)
    html = build_html_report(channel_a_info, channel_a_metrics, channel_b_info,
                             channel_b_metrics, claude_data, channel_a_comments,
                             channel_b_comments, channel_a_top_videos,
                             channel_b_top_videos, channel_a_velocity,
                             channel_b_velocity)

    return html
