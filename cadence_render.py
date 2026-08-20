#!/usr/bin/env python3
"""
cadence_render.py, Hey Mike branded rendering for Cadence.

The timeline is the signature element and the thing people screenshot, so it is
drawn as SVG rather than DOM boxes: it scales cleanly, exports sharp, and gives
exact control over collision handling.
"""

from datetime import datetime, timedelta

from cadence import is_extension

BLUE = "#2f76dd"
BLUE_DARK = "#1c3d7a"
BLUE_MID = "#4d8ae4"
BLUE_PALE = "#a8c4f0"
INK = "#1c1c1e"
MUTED = "#8e8e93"
LINE = "#e5e5ea"
DROUGHT = "#f2f2f7"

# A gap only reads as a real absence once it is well past the artist's own
# normal spacing, so shading is relative to their median rather than fixed.
DROUGHT_MULTIPLE = 2.5
DROUGHT_MIN_DAYS = 240

W, H = 1080, 320
PAD_L, PAD_R, PAD_T, PAD_B = 26, 26, 34, 46
BASE_Y = 196


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _d(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def build_timeline_svg(releases, cadence_stats=None):
    """Full career on one horizontal axis. Albums dominate, singles recede."""
    pts = []
    for r in releases:
        if r.get("release_date_precision") != "day":
            continue
        d = _d(r.get("release_date"))
        if not d:
            continue
        pts.append((d, r))
    if len(pts) < 2:
        return '<p style="color:#8e8e93">Not enough dated releases to draw a timeline.</p>'

    pts.sort(key=lambda x: x[0])
    t0, t1 = pts[0][0], pts[-1][0]
    span = max((t1 - t0).days, 1)
    plot_w = W - PAD_L - PAD_R

    def x_of(d):
        return PAD_L + (d - t0).days / span * plot_w

    # ── drought shading ──────────────────────────────────────────────────────
    med = (cadence_stats or {}).get("median_gap_days") or 60
    threshold = max(med * DROUGHT_MULTIPLE, DROUGHT_MIN_DAYS)
    droughts = ""
    days = sorted({d.date() for d, _ in pts})
    for i in range(1, len(days)):
        gap = (days[i] - days[i - 1]).days
        if gap < threshold:
            continue
        x1 = x_of(datetime.combine(days[i - 1], datetime.min.time()))
        x2 = x_of(datetime.combine(days[i], datetime.min.time()))
        droughts += (f'<rect x="{x1:.1f}" y="{PAD_T}" width="{max(x2-x1,1):.1f}" '
                     f'height="{BASE_Y - PAD_T + 8}" fill="{DROUGHT}" rx="4"/>')
        if x2 - x1 > 74:
            droughts += (f'<text x="{(x1+x2)/2:.1f}" y="{PAD_T + 15}" text-anchor="middle" '
                         f'font-family="Anton, sans-serif" font-size="10" fill="{MUTED}" '
                         f'letter-spacing="1">{gap} DAYS QUIET</text>')

    # ── year gridlines ───────────────────────────────────────────────────────
    grid = ""
    step = 1 if span / 365 <= 12 else 2
    for yr in range(t0.year, t1.year + 1):
        if (yr - t0.year) % step:
            continue
        d = datetime(yr, 1, 1)
        if d < t0 or d > t1:
            continue
        x = x_of(d)
        grid += (f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{BASE_Y+8}" '
                 f'stroke="{LINE}" stroke-width="1"/>'
                 f'<text x="{x:.1f}" y="{BASE_Y+30}" text-anchor="middle" '
                 f'font-family="Anton, sans-serif" font-size="12" fill="{MUTED}" '
                 f'letter-spacing="1">{yr}</text>')

    axis = (f'<line x1="{PAD_L}" y1="{BASE_Y}" x2="{W-PAD_R}" y2="{BASE_Y}" '
            f'stroke="{LINE}" stroke-width="2"/>')

    # ── marks: singles first so albums always sit on top ─────────────────────
    singles, eps, albums = [], [], []
    for d, r in pts:
        t = r.get("inferred_type")
        if t == "album" and not is_extension(r.get("name", "")):
            albums.append((d, r))
        elif t == "album":
            eps.append((d, r))           # extensions ride at EP weight
        elif t == "ep":
            eps.append((d, r))
        else:
            singles.append((d, r))

    marks = ""
    for d, r in singles:
        marks += (f'<circle cx="{x_of(d):.1f}" cy="{BASE_Y}" r="3.4" fill="{BLUE_PALE}">'
                  f'<title>{_esc(r["name"])} · {r["release_date"]} · single</title></circle>')
    for d, r in eps:
        marks += (f'<circle cx="{x_of(d):.1f}" cy="{BASE_Y}" r="6" fill="{BLUE_MID}">'
                  f'<title>{_esc(r["name"])} · {r["release_date"]} · '
                  f'{r["total_tracks"]} tracks</title></circle>')

    # ── album stems + labels, with collision avoidance ───────────────────────
    lanes = [0, 1, 2]
    # Wider catalogs need more horizontal separation before a label is safe.
    min_spacing = 118 if len(albums) <= 12 else 150
    last_x = {l: -999 for l in lanes}
    lane_y = {0: BASE_Y - 118, 1: BASE_Y - 84, 2: BASE_Y - 50}
    album_marks = ""
    for d, r in albums:
        x = x_of(d)
        lane = next((l for l in lanes if x - last_x[l] > min_spacing), None)
        name = r.get("name", "")
        label = name if len(name) <= 22 else name[:21] + "…"
        if lane is None:
            album_marks += (f'<circle cx="{x:.1f}" cy="{BASE_Y}" r="8" fill="{BLUE}" '
                            f'stroke="#fff" stroke-width="2">'
                            f'<title>{_esc(name)} · {r["release_date"]}</title></circle>')
            continue
        last_x[lane] = x
        y = lane_y[lane]
        # Labels are centre-anchored, so ones near either edge would overflow
        # the viewBox and get clipped. Pull the text back while the stem and
        # dot stay on the true date.
        half = max(34, min(64, len(label) * 3.4))
        lx = min(max(x, PAD_L + half), W - PAD_R - half)
        album_marks += (
            f'<line x1="{lx:.1f}" y1="{y+14}" x2="{x:.1f}" y2="{BASE_Y-8}" '
            f'stroke="{BLUE_PALE}" stroke-width="1.5"/>'
            f'<circle cx="{x:.1f}" cy="{BASE_Y}" r="8.5" fill="{BLUE}" '
            f'stroke="#fff" stroke-width="2"><title>{_esc(name)} · '
            f'{r["release_date"]} · {r["total_tracks"]} tracks</title></circle>'
            f'<text x="{lx:.1f}" y="{y}" text-anchor="middle" font-family="Anton, sans-serif" '
            f'font-size="12.5" fill="{INK}" letter-spacing=".3">{_esc(label.upper())}</text>'
            f'<text x="{lx:.1f}" y="{y+13}" text-anchor="middle" font-family="Inter, sans-serif" '
            f'font-size="10.5" fill="{MUTED}">{r["release_date"][:4]}</text>')

    legend = (
        f'<g transform="translate({PAD_L},{H-12})" font-family="Inter, sans-serif" font-size="11.5">'
        f'<circle cx="6" cy="-4" r="8.5" fill="{BLUE}"/><text x="21" y="0" fill="{INK}">Album</text>'
        f'<circle cx="82" cy="-4" r="6" fill="{BLUE_MID}"/><text x="94" y="0" fill="{INK}">EP / short project</text>'
        f'<circle cx="212" cy="-4" r="3.4" fill="{BLUE_PALE}"/><text x="222" y="0" fill="{INK}">Single</text>'
        f'<rect x="272" y="-11" width="15" height="13" fill="{DROUGHT}" rx="3"/>'
        f'<text x="294" y="0" fill="{INK}">Extended quiet period</text></g>')

    return (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
            f'aria-label="Release timeline" preserveAspectRatio="xMidYMid meet">'
            f'{droughts}{grid}{axis}{marks}{album_marks}{legend}</svg>')


# ── Report page ──────────────────────────────────────────────────────────────

TAG_LABEL = {"how often": "How often", "when": "When", "rollout": "Album rollout",
             "catalog": "Catalog", "business": "Business"}


def _stat(value, label):
    return (f'<div class="cd-stat"><b>{_esc(value)}</b>'
            f'<span>{_esc(label)}</span></div>')


def _takeaway_html(t, i):
    return (f'<div class="cd-take"><div class="cd-take-n">{i}</div>'
            f'<div><div class="cd-take-h">{_esc(t["headline"])}</div>'
            f'<div class="cd-take-d">{_esc(t["detail"])}</div>'
            f'<span class="cd-chip">{_esc(TAG_LABEL.get(t["tag"], t["tag"]))}</span>'
            f'<span class="cd-chip cd-chip-b">{_esc(t["stat"])}</span></div></div>')


def _share_bar(share_id):
    if not share_id:
        return ""
    return f'''
<div class="cd-share">
  <div class="cd-share-t">Shareable link</div>
  <div class="cd-share-r">
    <input id="shareUrl" readonly value="" onclick="this.select()">
    <button onclick="cdCopy()" id="copyBtn">Copy</button>
  </div>
  <div class="cd-share-n">Anyone with this link can open this report. It stays live and
  costs nothing to view, so it is safe to post publicly or send to as many people as
  you like.</div>
</div>
<script>
  document.getElementById('shareUrl').value =
    window.location.origin + '/r/{share_id}';
  function cdCopy(){{
    var f = document.getElementById('shareUrl');
    f.select(); f.setSelectionRange(0, 99999);
    navigator.clipboard.writeText(f.value).then(function(){{
      var b = document.getElementById('copyBtn');
      b.textContent = 'Copied'; setTimeout(function(){{ b.textContent = 'Copy'; }}, 1800);
    }});
  }}
</script>'''


def build_report(artist, releases, cadence_stats, rhythm, ramp, extensions,
                 labels, dropday, takeaways, projection, merch_html="",
                 recent_takeaways=None, share_id=None):
    a = artist
    c, ry = cadence_stats, rhythm or {}

    nm = a.get("name") or "This artist"
    if ry.get("enough_data"):
        hero_read = (f'{_esc(nm)} has put out {ry["total"]} releases over '
                     f'{ry["career_years"]} years, averaging {ry["releases_per_year"]} a year. '
                     "The numbers below are the typical spacing between them.")
    else:
        hero_read = f'{_esc(nm)} does not have enough dated releases to read a pattern yet.'

    takes = "".join(_takeaway_html(t, i) for i, t in enumerate(takeaways.get("takeaways", []), 1)) \
        or f'<p class="cd-muted">{_esc(takeaways.get("summary", ""))}</p>'

    rt = recent_takeaways or {}
    takes_recent = "".join(_takeaway_html(t, i) for i, t in enumerate(rt.get("takeaways", []), 1))
    if takes_recent:
        takes_recent = (f'<p class="cd-foot" style="margin-bottom:14px">Based only on the '
                        f'{rt.get("window_count", 0)} releases from the last '
                        f'{rt.get("window_months", 24)} months. Label history and career-long '
                        "trends are excluded here, since they need more than two years to mean "
                        f"anything.</p>{takes_recent}")
    else:
        takes_recent = f'<p class="cd-muted">{_esc(rt.get("summary", "No recent releases."))}</p>'

    hero = "".join([
        _stat(str(ry.get("releases_per_year", "-")), "Releases per year"),
        _stat(f'{ry.get("single_median_gap_days", "-")} days', "Typical gap between singles"),
        _stat(f'{ry.get("album_median_gap_days", "-")} days', "Typical gap between albums"),
        _stat(str(ry.get("total", len(releases))), "Career releases"),
    ])

    # ── Ramp tab ──
    ramp_rows = ""
    for al in (ramp or {}).get("per_album", []):
        leads = "".join(
            f'<li><b>{s["days_before"]} days before</b>, {_esc(s["name"])}</li>'
            for s in al["lead_singles"])
        buf = "".join(
            f'<li class="cd-buf">{_esc(s["name"])}, released {s["days_before"]} days '
            "before, never made the album</li>" for s in al["buffet"])
        ramp_rows += (
            f'<div class="cd-ramp"><div class="cd-ramp-h">'
            f'<a href="{_esc(al["album_url"])}" target="_blank" rel="noopener">'
            f'{_esc(al["album"])}</a> <span>{al["album_date"]}</span></div>'
            f'<ul>{leads}{buf}</ul></div>')
    cr = (ramp or {}).get("career", {})
    ramp_head = (
        f'<p class="cd-read">Across {cr.get("albums_with_lead_singles", 0)} album campaigns, '
        f'the first single arrives about <b>{cr.get("avg_ramp_days", "-")} days</b> before the '
        f'album, with roughly <b>{cr.get("avg_lead_singles", "-")}</b> lead singles each.</p>'
    ) if cr.get("albums_with_lead_singles") else \
        '<p class="cd-muted">No singles were released in the 180 days before any album.</p>'

    # ── Cadence tab ──
    yrs = ry.get("releases_by_year") or {}
    ymax = max(yrs.values()) if yrs else 1
    ybars = "".join(
        f'<div class="cd-ybar"><span class="cd-ybar-l">{y}</span>'
        f'<span class="cd-ybar-t"><i style="width:{n/ymax*100:.0f}%"></i></span>'
        f'<span class="cd-ybar-n">{n}</span></div>' for y, n in yrs.items())
    # Per-year split so "releases per year" is not ambiguous about what it counts.
    ytype = ""
    counts = {}
    for r in releases:
        y = r.get("release_year")
        if not y:
            continue
        t = r.get("inferred_type", "single")
        bucket = "albums" if t == "album" else ("EPs" if t == "ep" else "singles")
        counts.setdefault(y, {"albums": 0, "EPs": 0, "singles": 0})
        counts[y][bucket] += 1
    if counts:
        rows = "".join(
            f'<tr><td>{y}</td><td class="cd-num">{v["albums"]}</td>'
            f'<td class="cd-num">{v["EPs"]}</td><td class="cd-num">{v["singles"]}</td></tr>'
            for y, v in sorted(counts.items(), reverse=True))
        ytype = ('<table><thead><tr><th>Year</th><th class="cd-num">Albums</th>'
                 '<th class="cd-num">EPs</th><th class="cd-num">Singles</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table>')

    dd = dropday or {}
    dmax = max(dd.get("by_day", {}).values()) if dd.get("by_day") else 1
    dbars = "".join(
        f'<div class="cd-ybar"><span class="cd-ybar-l">{d}</span>'
        f'<span class="cd-ybar-t"><i style="width:{n/dmax*100:.0f}%;'
        f'background:{BLUE if d=="Friday" else BLUE_PALE}"></i></span>'
        f'<span class="cd-ybar-n">{n}</span></div>'
        for d, n in (dd.get("by_day") or {}).items())

    # ── Catalog tab ──
    x = extensions or {}
    ext_rows = ""
    for l in x.get("linked", []):
        when = ("released the same day" if l["gap_days"] == 0
                else f'{l["gap_days"]} days after the original')
        ext_rows += (f'<tr><td>{_esc(l["original"])}</td><td>{_esc(l["extension"])}</td>'
                     f'<td class="cd-num">{when}</td></tr>')

    lt = dd.get("album_length_by_year") or {}
    lmax = max(lt.values()) if lt else 1
    lbars = "".join(
        f'<div class="cd-ybar"><span class="cd-ybar-l">{y}</span>'
        f'<span class="cd-ybar-t"><i style="width:{v/lmax*100:.0f}%"></i></span>'
        f'<span class="cd-ybar-n">{v:g}</span></div>' for y, v in lt.items())

    # ── Business tab ──
    lb = labels or {}
    path = ""
    if lb.get("changes"):
        seq = [lb["changes"][0]["from"]] + [ch["to"] for ch in lb["changes"]]
        path = '<div class="cd-path">' + "".join(
            f'<span>{_esc(s)}</span>' for s in seq) + "</div>"
    lab_rows = "".join(
        f'<tr><td>{t["date"]}</td><td>{_esc(t["release"])}</td>'
        f'<td>{_esc(t.get("label_primary") or t["label"])}</td>'
        f'<td class="cd-raw">{_esc(t["raw_copyright"][:150])}</td></tr>'
        for t in lb.get("timeline", []))

    # ── Projection tab ──
    proj = "".join(
        f'<div class="cd-win"><b>{_esc(w["month"])}</b>'
        f'<span>{w["from"]} → {w["to"]}</span></div>'
        for w in (projection or {}).get("windows", []))
    proj_note = (projection or {}).get("album_note") or ""

    merch_tab = (f'<div class="tab-panel" id="p-merch">{merch_html}</div>'
                 if merch_html else "")
    merch_btn = ('<button class="tab-btn" onclick="showTab(\'merch\',this)">Merch</button>'
                 if merch_html else "")

    img = (f'<img class="cd-avatar" src="{_esc(a.get("image"))}" alt="">'
           if a.get("image") else "")

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(a.get("name",""))}, Cadence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:#f9f9fb;
      color:{INK};padding:24px;display:flex;flex-direction:column;min-height:100vh}}
.container{{max-width:1140px;margin:0 auto;width:100%;flex:1}}
.cd-head{{display:flex;align-items:center;gap:18px;margin-bottom:18px;flex-wrap:wrap}}
.cd-avatar{{width:74px;height:74px;border-radius:50%;object-fit:cover;background:#f2f2f7}}
h1{{font-family:'Anton',sans-serif;text-transform:uppercase;letter-spacing:.02em;
   font-size:34px;font-weight:400}}
.cd-sub{{font-size:14px;color:{MUTED};margin-top:2px}}
.cd-sub a{{color:{BLUE};text-decoration:none}}
.card{{background:#fff;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.06);
       padding:24px;margin-bottom:16px}}
.card-title{{font-family:'Anton',sans-serif;text-transform:uppercase;letter-spacing:.02em;
             font-size:15px;font-weight:400;margin-bottom:14px}}
.cd-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}}
.cd-stat{{background:#fff;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.06);
          padding:20px 16px;text-align:center}}
.cd-stat b{{display:block;font-family:'Anton',sans-serif;font-size:34px;font-weight:400;
            color:{BLUE};line-height:1}}
.cd-stat span{{display:block;margin-top:7px;font-size:11.5px;color:{MUTED};line-height:1.35}}
.tab-bar{{display:flex;gap:4px;background:#f2f2f7;border-radius:12px;padding:4px;
          margin-bottom:16px;overflow-x:auto}}
.tab-btn{{flex:1;min-width:104px;padding:11px 14px;border:none;border-radius:9px;
          background:transparent;font-family:'Anton',sans-serif;text-transform:uppercase;
          letter-spacing:.02em;font-size:14px;color:#6c6c70;cursor:pointer;transition:all .15s}}
.tab-btn.active{{background:#fff;color:{BLUE};box-shadow:0 2px 8px rgba(47,118,221,.15)}}
.tab-panel{{display:none}} .tab-panel.active{{display:block}}
.cd-take{{display:flex;gap:14px;padding:16px 0;border-bottom:1px solid #f2f2f7}}
.cd-take:last-child{{border-bottom:none}}
.cd-take-n{{flex-shrink:0;width:28px;height:28px;border-radius:50%;background:{BLUE};color:#fff;
            font-family:'Anton',sans-serif;font-size:14px;display:flex;align-items:center;
            justify-content:center}}
.cd-take-h{{font-size:17px;font-weight:600;line-height:1.4;margin-bottom:5px}}
.cd-take-d{{font-size:14.5px;line-height:1.6;color:#3c3c43;margin-bottom:8px}}
.cd-chip{{display:inline-block;background:#f2f2f7;color:#6c6c70;font-size:11px;
          padding:3px 9px;border-radius:999px;margin-right:6px}}
.cd-chip-b{{background:#e6eefc;color:{BLUE};font-weight:600}}
.cd-share{{background:#e6eefc;border-radius:14px;padding:16px 18px;margin-bottom:16px}}
.cd-share-t{{font-family:'Anton',sans-serif;text-transform:uppercase;letter-spacing:.06em;
             font-size:11px;color:{BLUE};margin-bottom:8px}}
.cd-share-r{{display:flex;gap:8px}}
.cd-share-r input{{flex:1;min-width:0;height:40px;border-radius:9px;border:1px solid #c9dbf7;
             padding:0 12px;font-size:13.5px;font-family:inherit;background:#fff;color:{INK}}}
.cd-share-r button{{height:40px;padding:0 20px;border:none;border-radius:9px;background:{BLUE};
             color:#fff;font-family:'Anton',sans-serif;text-transform:uppercase;
             letter-spacing:.04em;font-size:13px;cursor:pointer;flex-shrink:0}}
.cd-share-n{{font-size:12px;color:#3c3c43;line-height:1.5;margin-top:9px}}
@media(max-width:700px){{.cd-share-r{{flex-direction:column}}
  .cd-share-r button{{width:100%}}}}
.cd-hero-read{{font-size:15.5px;line-height:1.6;color:#3c3c43;margin-bottom:14px;max-width:760px}}
.cd-read{{font-size:15.5px;line-height:1.6;color:#3c3c43;margin-bottom:14px}}
.cd-muted{{color:{MUTED};font-size:14px}}
.cd-ybar{{display:flex;align-items:center;gap:12px;margin-bottom:7px}}
.cd-ybar-l{{width:74px;font-size:13px;color:#6c6c70}}
.cd-ybar-t{{flex:1;background:#f2f2f7;border-radius:999px;height:10px;overflow:hidden}}
.cd-ybar-t i{{display:block;height:100%;background:{BLUE};border-radius:999px}}
.cd-ybar-n{{width:34px;text-align:right;font-size:13px;font-weight:600}}
.cd-ramp{{padding:14px 0;border-bottom:1px solid #f2f2f7}}
.cd-ramp:last-child{{border-bottom:none}}
.cd-ramp-h{{font-family:'Anton',sans-serif;text-transform:uppercase;font-size:16px;
            letter-spacing:.02em;margin-bottom:6px}}
.cd-ramp-h a{{color:{INK};text-decoration:none}} .cd-ramp-h a:hover{{color:{BLUE}}}
.cd-ramp-h span{{font-family:'Inter',sans-serif;font-size:12px;color:{MUTED};
                 text-transform:none;letter-spacing:0;margin-left:6px}}
.cd-ramp ul{{list-style:none;padding-left:2px}}
.cd-ramp li{{font-size:14px;color:#3c3c43;padding:3px 0}}
.cd-ramp li b{{color:{BLUE}}}
.cd-buf{{color:{MUTED}!important;font-style:italic}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th{{text-align:left;font-family:'Anton',sans-serif;text-transform:uppercase;font-size:11px;
    letter-spacing:.06em;color:{MUTED};padding:8px 6px;border-bottom:1px solid #e5e5ea}}
td{{padding:9px 6px;border-bottom:1px solid #f2f2f7;vertical-align:top}}
.cd-num{{text-align:right;color:{BLUE};font-weight:600;white-space:nowrap}}
.cd-raw{{color:{MUTED};font-size:11.5px}}
.cd-path{{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:16px}}
.cd-path span{{background:#e6eefc;color:{BLUE};font-family:'Anton',sans-serif;
               text-transform:uppercase;font-size:13px;letter-spacing:.02em;
               padding:7px 13px;border-radius:999px;position:relative}}
.cd-path span:not(:last-child)::after{{content:"→";position:absolute;right:-15px;
               color:{MUTED};font-family:'Inter',sans-serif}}
.cd-wins{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}}
.cd-win{{background:#f9f9fb;border-radius:12px;padding:14px;text-align:center}}
.cd-win b{{display:block;font-family:'Anton',sans-serif;font-size:17px;color:{BLUE}}}
.cd-win span{{display:block;font-size:11px;color:{MUTED};margin-top:4px}}
.cd-note{{background:#e6eefc;border-radius:12px;padding:14px 18px;font-size:14px;
          line-height:1.6;color:#1c3d7a;margin-top:14px}}
.cd-foot{{font-size:12.5px;color:{MUTED};line-height:1.6;margin-top:8px}}
{TIMELINE_CSS}
.hey-mike-footer{{text-align:right;max-width:1140px;margin:24px auto 0;width:100%;
  font-family:'Anton',sans-serif;text-transform:uppercase;letter-spacing:.02em;
  font-size:33px;color:{BLUE}}}
@media(max-width:700px){{
  .cd-stats{{grid-template-columns:repeat(2,1fr)}}
  .hey-mike-footer{{text-align:center;font-size:24px}}
  h1{{font-size:26px}}
}}
</style></head><body><div class="container">

<div class="cd-head">{img}
  <div><h1>{_esc(a.get("name",""))}</h1>
    <div class="cd-sub">Release strategy · data pinned to the US market ·
      <a href="{_esc(a.get("url",""))}" target="_blank" rel="noopener">Spotify profile</a></div>
  </div>
</div>

<p class="cd-hero-read">{hero_read}</p>
<div class="cd-stats">{hero}</div>
{_share_bar(share_id)}

<div class="tab-bar">
  <button class="tab-btn active" onclick="showTab('take',this)">Takeaways</button>
  <button class="tab-btn" onclick="showTab('time',this)">Timeline</button>
  <button class="tab-btn" onclick="showTab('cad',this)">Cadence</button>
  <button class="tab-btn" onclick="showTab('cat',this)">Catalog</button>
  <button class="tab-btn" onclick="showTab('biz',this)">Business</button>
  {merch_btn}
</div>

<div class="tab-panel active" id="p-take"><div class="card">
  <div class="tl-sub">
    <button class="tl-card active" onclick="showTk('career',this)">
      <b>Whole Career</b><span>Every year of releases</span></button>
    <button class="tl-card" onclick="showTk('recent',this)">
      <b>Last 24 Months</b><span>How they are operating right now</span></button>
  </div>
  <div class="tl-view active" id="tk-career">{takes}</div>
  <div class="tl-view" id="tk-recent">{takes_recent}</div>
</div></div>

<div class="tab-panel" id="p-time"><div class="card">
  <div class="tl-sub">
    <button class="tl-card active" onclick="showTl('recent',this)">
      <b>Last 24 Months</b><span>Every release, dated, newest first</span></button>
    <button class="tl-card" onclick="showTl('career',this)">
      <b>Career</b><span>Year by year, with cover art</span></button>
  </div>
  <div class="tl-view active" id="tl-recent">{build_recent_list(releases)}</div>
  <div class="tl-view" id="tl-career">{build_career_years(releases)}</div>
</div></div>

<div class="tab-panel" id="p-cad">
  <div class="card"><div class="card-title">Releases per year</div>
    <p class="cd-foot" style="margin-bottom:12px">Counts every release, albums, EPs and
    singles together. Compilations are excluded.</p>{ybars}
    <div style="margin-top:18px">{ytype}</div></div>
  <div class="card"><div class="card-title">Which day of the week</div>{dbars}
    <p class="cd-foot">Friday is the global standard release day
    ({dd.get("friday_pct", 0)}% here).</p></div>
</div>

<div class="tab-panel" id="p-cat">
  <div class="card"><div class="card-title">Deluxes, reissues and remixes</div>
    <p class="cd-read">{x.get("extension_count",0)} extensions across
      {x.get("original_album_count",0)} original albums. On average a record keeps getting
      worked for <b>{x.get("avg_days_working_a_record","-")} days</b> after release.</p>
    <table><thead><tr><th>Original album</th><th>Later version</th><th>When</th></tr></thead>
    <tbody>{ext_rows or '<tr><td colspan="3" class="cd-muted">No extensions found.</td></tr>'}</tbody></table>
  </div>
  <div class="card"><div class="card-title">Tracks per album, by year</div>
    <p class="cd-foot" style="margin-bottom:12px">Average number of tracks on albums released
    in each year, showing whether records are getting longer or shorter.</p>{lbars or
    '<p class="cd-muted">Not enough albums to show a trend.</p>'}</div>
</div>

<div class="tab-panel" id="p-biz"><div class="card">
  <div class="card-title">Label trajectory</div>{path}
  <table><thead><tr><th>Date</th><th>Release</th><th>Label</th><th>Copyright line</th></tr></thead>
  <tbody>{lab_rows or '<tr><td colspan="4" class="cd-muted">No copyright data available.</td></tr>'}</tbody></table>
  <p class="cd-foot">Labels are read from the ℗ copyright line, since Spotify does not expose a
  label field to this application. The raw line is shown because the licensing wording in it is
  often the most revealing part.</p>
</div></div>

{merch_tab}

<div class="card"><p class="cd-foot"><b>What this cannot see.</b> Cadence reads public release
metadata only. It cannot see streams, saves, playlist adds or monthly listeners, which data
lives in Spotify for Artists. This is a strategy X-ray, not a performance dashboard.</p></div>

</div>
<div class="hey-mike-footer">Powered by Hey Mike</div>
<script>
function showTk(n,el){{
  document.querySelectorAll('#p-take .tl-view').forEach(v=>v.classList.remove('active'));
  document.querySelectorAll('#p-take .tl-card').forEach(b=>b.classList.remove('active'));
  document.getElementById('tk-'+n).classList.add('active');
  el.classList.add('active');
}}
function showTl(n,el){{
  document.querySelectorAll('#p-time .tl-view').forEach(v=>v.classList.remove('active'));
  document.querySelectorAll('#p-time .tl-card').forEach(b=>b.classList.remove('active'));
  document.getElementById('tl-'+n).classList.add('active');
  el.classList.add('active');
}}
function showTab(n,el){{
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('p-'+n).classList.add('active');
  el.classList.add('active');
}}
</script></body></html>"""


# ── Timeline views ───────────────────────────────────────────────────────────

TYPE_LABEL = {"album": "ALBUM", "ep": "EP", "single": "SINGLE", "compilation": "COMP"}
TYPE_COLOR = {"album": BLUE, "ep": BLUE_MID, "single": BLUE_PALE, "compilation": MUTED}


def _cover(url, size=300):
    if not url:
        return '<div class="tl-noart"></div>'
    return f'<img src="{_esc(url)}" alt="" loading="lazy">'


def _fmt_us(datestr):
    d = _d(datestr)
    return d.strftime("%-m/%-d/%y") if d else datestr


def build_recent_list(releases, months=24):
    """Dated list of everything from the last N months, newest first."""
    cutoff = datetime.utcnow() - timedelta(days=months * 30.4)
    rows = []
    for r in releases:
        d = _d(r.get("release_date"))
        if not d or d < cutoff:
            continue
        rows.append((d, r))
    if not rows:
        return ('<p class="cd-muted">Nothing released in the last '
                f'{months} months.</p>')

    rows.sort(key=lambda x: -x[0].timestamp())
    out = ""
    for d, r in rows:
        t = r.get("inferred_type", "single")
        ext = " · deluxe/reissue" if is_extension(r.get("name", "")) else ""
        out += (
            f'<a class="tl-row" href="{_esc(r.get("url",""))}" target="_blank" rel="noopener">'
            f'<span class="tl-date">{_fmt_us(r["release_date"])}</span>'
            f'<span class="tl-art">{_cover(r.get("image"))}</span>'
            f'<span class="tl-badge" style="background:{TYPE_COLOR.get(t, MUTED)}">'
            f'{TYPE_LABEL.get(t, t.upper())}</span>'
            f'<span class="tl-name">{_esc(r.get("name",""))}'
            f'<i class="tl-meta">{r.get("total_tracks",0)} '
            f'track{"" if r.get("total_tracks")==1 else "s"}{ext}</i></span></a>')
    return f'<div class="tl-list">{out}</div>'


def build_career_years(releases):
    """
    Year by year, with cover art. Albums and EPs get tiles because those are the
    events people remember; singles are summarized as a count so a prolific year
    doesn't drown the page in thumbnails.
    """
    years = {}
    for r in releases:
        y = r.get("release_year")
        if not y:
            continue
        years.setdefault(y, {"major": [], "singles": 0})
        if r.get("inferred_type") in ("album", "ep", "compilation"):
            years[y]["major"].append(r)
        else:
            years[y]["singles"] += 1
    if not years:
        return '<p class="cd-muted">No dated releases to show.</p>'

    out = ""
    for y in sorted(years, reverse=True):
        blk = years[y]
        blk["major"].sort(key=lambda r: r.get("release_date", ""))
        tiles = ""
        for r in blk["major"]:
            t = r.get("inferred_type", "album")
            tiles += (
                f'<a class="tl-tile" href="{_esc(r.get("url",""))}" target="_blank" rel="noopener">'
                f'<span class="tl-tile-art">{_cover(r.get("image"))}'
                f'<i class="tl-tile-badge" style="background:{TYPE_COLOR.get(t, MUTED)}">'
                f'{TYPE_LABEL.get(t, t.upper())}</i></span>'
                f'<span class="tl-tile-n">{_esc(r.get("name",""))}</span>'
                f'<span class="tl-tile-d">{_fmt_us(r.get("release_date",""))} · '
                f'{r.get("total_tracks",0)} tracks</span></a>')
        if not tiles:
            tiles = '<p class="cd-muted" style="padding:6px 0">No albums or EPs this year.</p>'
        sing = (f'<span class="tl-year-s">+ {blk["singles"]} single'
                f'{"" if blk["singles"]==1 else "s"}</span>') if blk["singles"] else ""
        out += (f'<div class="tl-year"><div class="tl-year-h"><b>{y}</b>{sing}</div>'
                f'<div class="tl-tiles">{tiles}</div></div>')
    return out


TIMELINE_CSS = """
.tl-sub{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px}
.tl-card{background:#f9f9fb;border:2px solid transparent;border-radius:14px;padding:16px 18px;
         cursor:pointer;text-align:left;transition:all .15s;font-family:inherit}
.tl-card:hover{background:#f2f2f7}
.tl-card.active{background:#fff;border-color:#2f76dd;box-shadow:0 2px 10px rgba(47,118,221,.15)}
.tl-card b{display:block;font-family:'Anton',sans-serif;text-transform:uppercase;
           letter-spacing:.02em;font-size:15px;color:#1c1c1e}
.tl-card span{display:block;font-size:12.5px;color:#8e8e93;margin-top:3px;line-height:1.4}
.tl-view{display:none}.tl-view.active{display:block}
.tl-list{display:flex;flex-direction:column}
.tl-row{display:flex;align-items:center;gap:14px;padding:10px 4px;
        border-bottom:1px solid #f2f2f7;text-decoration:none;color:inherit}
.tl-row:last-child{border-bottom:none}
.tl-row:hover{background:#f9f9fb;border-radius:8px}
.tl-date{width:74px;flex-shrink:0;font-family:'Anton',sans-serif;font-size:14px;color:#8e8e93;
         letter-spacing:.02em}
.tl-art{width:46px;height:46px;flex-shrink:0;border-radius:8px;overflow:hidden;background:#f2f2f7}
.tl-art img{width:100%;height:100%;object-fit:cover;display:block}
.tl-noart{width:100%;height:100%;background:repeating-linear-gradient(45deg,#f2f2f7,
          #f2f2f7 6px,#eaeaef 6px,#eaeaef 12px)}
.tl-badge{flex-shrink:0;color:#fff;font-family:'Anton',sans-serif;font-size:10px;
          letter-spacing:.06em;padding:3px 9px;border-radius:999px;min-width:62px;text-align:center}
.tl-name{font-size:14.5px;color:#1c1c1e;line-height:1.35}
.tl-meta{display:block;font-style:normal;font-size:11.5px;color:#8e8e93;margin-top:1px}
.tl-year{margin-bottom:26px}
.tl-year-h{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid #e5e5ea;
           padding-bottom:7px;margin-bottom:14px}
.tl-year-h b{font-family:'Anton',sans-serif;font-size:24px;color:#2f76dd;letter-spacing:.02em}
.tl-year-s{font-size:12.5px;color:#8e8e93}
.tl-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:16px}
.tl-tile{text-decoration:none;color:inherit;display:block}
.tl-tile-art{position:relative;display:block;aspect-ratio:1/1;border-radius:10px;
             overflow:hidden;background:#f2f2f7}
.tl-tile-art img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .25s}
.tl-tile:hover .tl-tile-art img{transform:scale(1.05)}
.tl-tile-badge{position:absolute;top:7px;left:7px;color:#fff;font-family:'Anton',sans-serif;
               font-style:normal;font-size:9px;letter-spacing:.06em;padding:2px 7px;border-radius:999px}
.tl-tile-n{display:block;font-size:13px;line-height:1.35;margin-top:7px;color:#1c1c1e;
           display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tl-tile-d{display:block;font-size:11px;color:#8e8e93;margin-top:2px}
@media(max-width:700px){.tl-sub{grid-template-columns:1fr}
  .tl-date{width:58px;font-size:12.5px}.tl-badge{min-width:52px;font-size:9px}}
"""
