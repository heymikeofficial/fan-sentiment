#!/usr/bin/env python3
"""
cadence_app.py, Cadence web app (Spotify release-strategy X-ray).

Port 8083, alongside the other Hey Mike tools.

The Spotify credentials carry a hard daily quota, so caching is not a nicety
here. It is what keeps the tool alive under any real traffic. Two layers:
cadence.py caches raw discographies for 24h, and this file caches rendered
reports so a repeat lookup costs nothing at all.
"""

import os
import html
import hashlib
import sqlite3
from datetime import datetime, date, timedelta

from flask import Flask, request, Response
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.expanduser("~"), "Desktop", ".env"))

import cadence
import cadence_render
from merch import analyze_store, compare_stores, render_merch_tab, MerchError

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cadence_usage.db")
CACHE_TTL_HOURS = 24
GLOBAL_DAILY_LIMIT = 40      # Spotify quota is the real ceiling, not server load
IP_DAILY_LIMIT = 3

app = Flask(__name__)


# ── DB ───────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH, timeout=15) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ip TEXT,
            artist TEXT, artist_id TEXT, store TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS report_cache (
            cache_key TEXT PRIMARY KEY, html TEXT, created_at TEXT)""")


def cache_key(artist_id, store, compare_store):
    raw = "|".join([artist_id or "", (store or "").lower(), (compare_store or "").lower()])
    return hashlib.sha256(raw.encode()).hexdigest()


def get_cached(key):
    with sqlite3.connect(DB_PATH, timeout=15) as c:
        row = c.execute("SELECT html, created_at FROM report_cache WHERE cache_key=?",
                        (key,)).fetchone()
    if not row:
        return None
    try:
        made = datetime.fromisoformat(row[1])
    except ValueError:
        return None
    if datetime.utcnow() - made > timedelta(hours=CACHE_TTL_HOURS):
        return None
    return row[0]


def set_cached(key, body):
    with sqlite3.connect(DB_PATH, timeout=15) as c:
        c.execute("INSERT OR REPLACE INTO report_cache VALUES (?,?,?)",
                  (key, body, datetime.utcnow().isoformat()))


def log_run(ip, artist, artist_id, store):
    with sqlite3.connect(DB_PATH, timeout=15) as c:
        c.execute("INSERT INTO runs (ts,ip,artist,artist_id,store) VALUES (?,?,?,?,?)",
                  (datetime.utcnow().isoformat(), ip, artist, artist_id, store or ""))


def check_rate_limit(ip):
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH, timeout=15) as c:
        total = c.execute("SELECT COUNT(*) FROM runs WHERE ts LIKE ?",
                          (today + "%",)).fetchone()[0]
        mine = c.execute("SELECT COUNT(*) FROM runs WHERE ip=? AND ts LIKE ?",
                         (ip, today + "%")).fetchone()[0]
    if total >= GLOBAL_DAILY_LIMIT:
        return False, ("Cadence has hit its daily limit for everyone. Spotify caps how "
                       "much data this tool can pull each day. Try again tomorrow.")
    if mine >= IP_DAILY_LIMIT:
        return False, (f"You've run {IP_DAILY_LIMIT} reports today, which is the daily "
                       "limit per person. Try again tomorrow.")
    return True, ""


init_db()


# ── Pipeline ─────────────────────────────────────────────────────────────────

def build_full_report(artist_url, store_url="", compare_store_url=""):
    data = cadence.fetch_discography(artist_url)
    artist, releases = data["artist"], data["releases"]

    c = cadence.compute_cadence(releases)
    rhythm = cadence.compute_rhythm(releases)
    ramp = cadence.compute_ramp(releases)
    ext = cadence.compute_extensions(releases)
    labels = cadence.compute_labels(releases)
    dropday = cadence.compute_dropday(releases)
    takeaways = cadence.build_takeaways(c, ramp, ext, labels, dropday,
                                        rhythm=rhythm,
                                        artist_name=artist.get("name") or "This artist")
    projection = cadence.project_next_12_months(c, ramp)
    recent = cadence.build_recent_takeaways(
        releases, artist_name=artist.get("name") or "This artist")

    merch_html = ""
    if store_url:
        # Merch is optional and must never take the report down with it, a dead
        # store URL degrades to a message inside the tab, nothing more.
        try:
            a = analyze_store(store_url)
            b = analyze_store(compare_store_url) if compare_store_url else None
            merch_html = render_merch_tab(a, b, compare_stores(a, b) if b else None)
        except MerchError as e:
            merch_html = (f'<div class="card"><div class="card-title">Direct-to-fan store</div>'
                          f'<p class="cd-muted">{html.escape(str(e))}</p></div>')

    body = cadence_render.build_report(artist, releases, c, rhythm, ramp, ext,
                                       labels, dropday, takeaways, projection, merch_html,
                                       recent_takeaways=recent)
    return body, artist


# ── Landing page ─────────────────────────────────────────────────────────────

LANDING = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cadence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,sans-serif;background:#f9f9fb;color:#1c1c1e;
     min-height:100vh;display:flex;flex-direction:column;padding:24px}
.wrap{flex:1;display:flex;align-items:center;justify-content:center}
.card{background:#fff;border-radius:20px;box-shadow:0 2px 20px rgba(0,0,0,.08);
      padding:44px;max-width:580px;width:100%}
h1{font-family:'Anton',sans-serif;text-transform:uppercase;letter-spacing:.02em;
   font-size:40px;font-weight:400;line-height:1}
.sub{font-size:15.5px;color:#6c6c70;line-height:1.6;margin:14px 0 26px}
label{display:block;font-family:'Inter',sans-serif;font-size:14.5px;font-weight:600;
      color:#1c1c1e;margin-bottom:7px;letter-spacing:0}
.opt{color:#8e8e93;font-weight:400;font-size:13.5px}
input{width:100%;height:48px;border-radius:12px;border:1.5px solid rgba(0,0,0,.12);
      padding:0 15px;font-size:15px;font-family:inherit;outline:none;margin-bottom:18px;
      transition:border-color .15s}
input:focus{border-color:#2f76dd;box-shadow:0 0 0 3px rgba(47,118,221,.15)}
button{width:100%;height:50px;background:#2f76dd;color:#fff;border:none;border-radius:12px;
       font-family:'Anton',sans-serif;text-transform:uppercase;letter-spacing:.04em;
       font-size:16px;cursor:pointer;transition:background .15s}
button:hover{background:#2560b8}
button:disabled{opacity:.8;cursor:not-allowed}
.hint{font-size:12.5px;color:#8e8e93;margin:-10px 0 18px;line-height:1.5}
.load{display:none;margin-top:16px}
.bar{height:6px;background:#f2f2f7;border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;width:4%;background:linear-gradient(90deg,#2f76dd,#7aa5e8);
       border-radius:99px;transition:width .8s ease}
.lt{font-size:14px;font-weight:500;margin-bottom:8px}
.ls{font-size:12px;color:#8e8e93;margin-top:6px}
.err{display:none;margin-top:14px;padding:13px 16px;background:#fff2f4;
     border:1px solid rgba(217,48,37,.2);border-radius:10px;font-size:14px;color:#c0143c}
.foot{font-size:12px;color:#aeaeb2;line-height:1.6;margin-top:22px;
      border-top:1px solid #f2f2f7;padding-top:16px}
.hey-mike-footer{text-align:right;max-width:1140px;margin:24px auto 0;width:100%;
  font-family:'Anton',sans-serif;text-transform:uppercase;letter-spacing:.02em;
  font-size:33px;color:#2f76dd}
@media(max-width:700px){.hey-mike-footer{text-align:center;font-size:24px}
  .card{padding:28px}h1{font-size:30px}}
</style></head><body>
<div class="wrap"><div class="card">
  <h1>Cadence</h1>
  <p class="sub">Paste a Spotify artist link to see how often they release, what they
  release, when they drop it, and how they build to an album.</p>
  <form id="f">
    <label>Spotify artist link</label>
    <input id="artist" type="url" required autocomplete="off" spellcheck="false"
           placeholder="https://open.spotify.com/artist/...">
    <p class="hint">On the artist's page: ••• menu → Share → Copy link to artist.
    Artist names aren't accepted. They resolve to the wrong artist too often.</p>
    <label>Merch store <span class="opt">- optional</span></label>
    <input id="store" type="url" autocomplete="off" spellcheck="false"
           placeholder="https://theirstore.com">
    <label>Compare against another store <span class="opt">- optional</span></label>
    <input id="store2" type="url" autocomplete="off" spellcheck="false"
           placeholder="https://anotherstore.com">
    <button id="go" type="submit">Analyze release strategy</button>
    <div class="load" id="load">
      <div class="lt" id="lt">Reading the discography…</div>
      <div class="bar"><i id="bar"></i></div>
      <div class="ls" id="ls">Usually takes 15–40 seconds</div>
    </div>
    <div class="err" id="err"></div>
  </form>
  <p class="foot">Cadence reads public release metadata. It can't see streams, saves,
  playlist adds or monthly listeners, which lives in Spotify for Artists. This is a
  strategy X-ray, not a performance dashboard. Merch analysis works with Shopify stores.</p>
</div></div>
<div class="hey-mike-footer">Powered by Hey Mike</div>
<script>
const steps=[[10,'Reading the discography…','Paging through every release'],
 [32,'Removing duplicate pressings…','Reissues and regional variants collapse into one'],
 [55,'Measuring the release rhythm…','Gaps between singles, EPs and albums'],
 [74,'Matching singles to albums…','Working out how long each rollout ran'],
 [88,'Writing the takeaways…','Almost there']];
let timers=[];
const f=document.getElementById('f'),go=document.getElementById('go'),
 load=document.getElementById('load'),err=document.getElementById('err'),
 bar=document.getElementById('bar'),lt=document.getElementById('lt'),ls=document.getElementById('ls');
f.addEventListener('submit',async e=>{
  e.preventDefault();
  go.disabled=true;load.style.display='block';err.style.display='none';bar.style.width='4%';
  timers=steps.map(s=>setTimeout(()=>{bar.style.width=s[0]+'%';lt.textContent=s[1];ls.textContent=s[2];},s[0]/90*38000));
  try{
    const fd=new FormData();
    fd.append('artist',document.getElementById('artist').value);
    fd.append('store',document.getElementById('store').value);
    fd.append('store2',document.getElementById('store2').value);
    const r=await fetch('/analyze',{method:'POST',body:fd});
    if(!r.ok){let m='Something went wrong.';try{const j=await r.json();if(j.error)m=j.error;}catch(_){}
      throw new Error(m);}
    timers.forEach(clearTimeout);bar.style.width='100%';lt.textContent='Done, loading report…';
    const t=await r.text();
    setTimeout(()=>{document.open();document.write(t);document.close();},250);
  }catch(ex){
    timers.forEach(clearTimeout);go.disabled=false;load.style.display='none';
    err.textContent=ex.message;err.style.display='block';
  }
});
</script></body></html>"""


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return LANDING, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/analyze", methods=["POST"])
def analyze():
    artist_url = (request.form.get("artist") or "").strip()
    store = (request.form.get("store") or "").strip()
    store2 = (request.form.get("store2") or "").strip()
    ip = request.remote_addr or "unknown"

    if not artist_url:
        return {"error": "Paste a Spotify artist link to get started."}, 400
    try:
        artist_id = cadence.parse_artist_id(artist_url)
    except ValueError as e:
        return {"error": str(e)}, 400

    key = cache_key(artist_id, store, store2)
    hit = get_cached(key)
    if hit:
        return Response(hit, content_type="text/html; charset=utf-8")

    ok, why = check_rate_limit(ip)
    if not ok:
        return {"error": why}, 429

    try:
        body, artist = build_full_report(artist_url, store, store2)
    except cadence.SpotifyError as e:
        msg = str(e)
        if "429" in msg or "Rate limited" in msg:
            msg = ("Spotify's daily data limit for this tool has been reached. "
                   "It resets within 24 hours.")
        return {"error": msg}, 502
    except ValueError as e:
        return {"error": str(e)}, 400
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}, 500

    set_cached(key, body)
    log_run(ip, artist.get("name", ""), artist_id, store)
    return Response(body, content_type="text/html; charset=utf-8")


@app.route("/stats")
def stats():
    with sqlite3.connect(DB_PATH, timeout=15) as c:
        total = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        today = c.execute("SELECT COUNT(*) FROM runs WHERE ts LIKE ?",
                          (date.today().isoformat() + "%",)).fetchone()[0]
        cached = c.execute("SELECT COUNT(*) FROM report_cache").fetchone()[0]
        recent = c.execute("SELECT ts,artist,store FROM runs ORDER BY id DESC LIMIT 50").fetchall()
    rows = "".join(
        f"<tr><td>{html.escape(t[:16].replace('T',' '))}</td>"
        f"<td>{html.escape(a or '-')}</td><td>{html.escape(s or '-')}</td></tr>"
        for t, a, s in recent)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cadence, Stats</title>
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',sans-serif;background:#f9f9fb;color:#1c1c1e;padding:44px;
      display:flex;flex-direction:column;min-height:100vh}}
.c{{max-width:940px;margin:0 auto;width:100%;flex:1}}
h1{{font-family:'Anton',sans-serif;text-transform:uppercase;font-size:30px;font-weight:400}}
.s{{font-size:14px;color:#8e8e93;margin-bottom:26px}}
.cards{{display:flex;gap:14px;margin-bottom:26px;flex-wrap:wrap}}
.card{{background:#fff;border-radius:16px;padding:22px 26px;box-shadow:0 2px 12px rgba(0,0,0,.06);min-width:150px}}
.v{{font-family:'Anton',sans-serif;font-size:36px;color:#2f76dd}}
.l{{font-size:11px;color:#8e8e93;text-transform:uppercase;letter-spacing:.06em;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:16px;overflow:hidden;
       box-shadow:0 2px 12px rgba(0,0,0,.06)}}
th{{font-family:'Anton',sans-serif;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
    color:#8e8e93;padding:13px 18px;text-align:left;border-bottom:1px solid #f2f2f7}}
td{{font-size:13px;padding:12px 18px;border-bottom:1px solid #f7f7f9}}
.hm{{text-align:right;max-width:940px;margin:24px auto 0;width:100%;font-family:'Anton',sans-serif;
     text-transform:uppercase;font-size:33px;color:#2f76dd}}
</style></head><body><div class="c">
<h1>Cadence, Usage</h1><p class="s">Cached reports cost no Spotify quota.</p>
<div class="cards">
 <div class="card"><div class="v">{total:,}</div><div class="l">Total reports</div></div>
 <div class="card"><div class="v">{today:,}</div><div class="l">Today</div></div>
 <div class="card"><div class="v">{cached:,}</div><div class="l">Cached</div></div>
 <div class="card"><div class="v">{GLOBAL_DAILY_LIMIT - today}</div><div class="l">Left today</div></div>
</div>
<table><thead><tr><th>Time (UTC)</th><th>Artist</th><th>Store</th></tr></thead>
<tbody>{rows or '<tr><td colspan="3" style="text-align:center;color:#8e8e93;padding:30px">No runs yet</td></tr>'}</tbody></table>
</div><div class="hm">Powered by Hey Mike</div></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8083)), debug=False)
