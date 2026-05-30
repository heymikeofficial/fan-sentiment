import os
import html
import sqlite3
from datetime import datetime, date
from flask import Flask, request, Response
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.expanduser("~"), "Desktop", ".env"))

from fan_signal import run_single, build_html_report, extract_video_id

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                video_id TEXT,
                video_title TEXT,
                channel TEXT,
                total_comments INTEGER
            )
        """)

def log_run(video_id, video_title, channel, total_comments):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO runs (ts, video_id, video_title, channel, total_comments) VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(), video_id, video_title, channel, total_comments)
        )

def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        today_str = date.today().isoformat()
        today = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE ts LIKE ?", (today_str + "%",)
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT ts, video_title, channel, total_comments FROM runs ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return total, today, recent

init_db()

app = Flask(__name__)

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Fan Sentiment</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f2f2f7;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .card {
      background: #fff;
      border-radius: 20px;
      box-shadow: 0 2px 20px rgba(0,0,0,0.08);
      padding: 48px;
      max-width: 560px;
      width: 100%;
      margin: 24px;
    }

    .header {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.5px;
      color: #1c1c1e;
      margin-bottom: 0;
    }

    .subtitle {
      font-size: 15px;
      color: #6c6c70;
      line-height: 1.6;
      margin: 12px 0 28px;
    }

    .input-url {
      width: 100%;
      height: 48px;
      border-radius: 12px;
      border: 1.5px solid rgba(0,0,0,0.12);
      padding: 0 16px;
      font-size: 15px;
      font-family: inherit;
      outline: none;
      transition: border-color 0.15s;
      color: #1c1c1e;
    }

    .input-url:focus {
      border-color: #ff6b3d;
      box-shadow: 0 0 0 3px rgba(255,107,61,0.15);
    }

    .btn-analyze {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      height: 48px;
      margin-top: 14px;
      background: #ff6b3d;
      color: #fff;
      border: none;
      border-radius: 12px;
      font-weight: 600;
      font-size: 15px;
      font-family: inherit;
      cursor: pointer;
      transition: background 0.15s;
    }

    .btn-analyze:hover {
      background: #e85a2c;
    }

    .btn-analyze:disabled {
      cursor: not-allowed;
      opacity: 0.85;
    }

    .loading-wrap {
      display: none;
      flex-direction: column;
      gap: 10px;
      width: 100%;
      margin-top: 14px;
    }

    .loading-label {
      display: flex;
      align-items: center;
      gap: 8px;
      color: #ff6b3d;
      font-size: 14px;
      font-weight: 500;
    }

    .spinner {
      width: 16px;
      height: 16px;
      border: 2px solid rgba(255,107,61,0.25);
      border-top-color: #ff6b3d;
      border-radius: 50%;
      animation: spin 0.75s linear infinite;
      flex-shrink: 0;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    .progress-track {
      width: 100%;
      height: 4px;
      background: rgba(255,107,61,0.15);
      border-radius: 99px;
      overflow: hidden;
    }

    .progress-bar {
      height: 100%;
      width: 0%;
      background: #ff6b3d;
      border-radius: 99px;
      transition: width 0.6s ease;
    }

    .progress-steps {
      font-size: 12px;
      color: #8e8e93;
    }

    .error-msg {
      display: none;
      margin-top: 12px;
      font-size: 14px;
      color: #d93025;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <svg width="28" height="20" viewBox="0 0 18 13" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M17.6 2.03C17.4 1.29 16.83 0.71 16.1 0.5C14.7 0.13 9 0.13 9 0.13C9 0.13 3.3 0.13 1.9 0.5C1.17 0.71 0.6 1.29 0.4 2.03C0 3.44 0 6.38 0 6.38C0 6.38 0 9.32 0.4 10.73C0.6 11.47 1.17 12.03 1.9 12.24C3.3 12.61 9 12.61 9 12.61C9 12.61 14.7 12.61 16.1 12.24C16.83 12.03 17.4 11.47 17.6 10.73C18 9.32 18 6.38 18 6.38C18 6.38 18 3.44 17.6 2.03Z" fill="#FF0000"/>
        <path d="M7.18 9.07L11.88 6.38L7.18 3.69V9.07Z" fill="white"/>
      </svg>
      Fan Sentiment
    </div>
    <p class="subtitle">Paste the link to your YouTube video below for an analysis of your comments &amp; audience sentiment.</p>

    <form id="analyzeForm">
      <input
        class="input-url"
        type="url"
        id="urlInput"
        name="url"
        placeholder="https://www.youtube.com/watch?v=..."
        required
        autocomplete="off"
        spellcheck="false"
      />
      <button class="btn-analyze" type="submit" id="analyzeBtn">Analyze</button>
      <div class="loading-wrap" id="loadingWrap">
        <div class="loading-label">
          <div class="spinner"></div>
          <span id="loadingText">Fetching comments…</span>
        </div>
        <div class="progress-track">
          <div class="progress-bar" id="progressBar"></div>
        </div>
        <div class="progress-steps" id="progressSteps">This takes about 30–60 seconds for large videos</div>
      </div>
      <div class="error-msg" id="errorMsg"></div>
    </form>
  </div>

  <script>
    const form = document.getElementById('analyzeForm');
    const btn = document.getElementById('analyzeBtn');
    const loading = document.getElementById('loadingWrap');
    const errorMsg = document.getElementById('errorMsg');

    const progressBar = document.getElementById('progressBar');
    const loadingText = document.getElementById('loadingText');
    const progressSteps = document.getElementById('progressSteps');

    const steps = [
      { pct: 12, label: 'Fetching comments…',        note: 'Pulling up to 3,000 comments from YouTube' },
      { pct: 35, label: 'Reading the comments…',     note: 'Scanning for timestamps and fan activity' },
      { pct: 58, label: 'Mapping fan reactions…',    note: 'Building your reaction heatmap' },
      { pct: 75, label: 'Running AI analysis…',      note: 'Claude is reading fan sentiment & vision' },
      { pct: 88, label: 'Almost there…',             note: 'Putting together your report' },
    ];

    let stepTimers = [];

    function startProgress() {
      const totalMs = 70000;
      steps.forEach((s, i) => {
        const t = setTimeout(() => {
          progressBar.style.width = s.pct + '%';
          loadingText.textContent = s.label;
          progressSteps.textContent = s.note;
        }, (s.pct / 90) * totalMs);
        stepTimers.push(t);
      });
    }

    function stopProgress() {
      stepTimers.forEach(clearTimeout);
      stepTimers = [];
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();

      const url = document.getElementById('urlInput').value.trim();
      if (!url) return;

      btn.style.display = 'none';
      loading.style.display = 'flex';
      errorMsg.style.display = 'none';
      progressBar.style.width = '4%';
      startProgress();

      try {
        const formData = new FormData();
        formData.append('url', url);

        const res = await fetch('/analyze', { method: 'POST', body: formData });

        if (!res.ok) {
          let errText = 'Analysis failed. Please check the URL and try again.';
          try {
            const data = await res.json();
            if (data && data.error) errText = data.error;
          } catch (_) { errText = await res.text() || errText; }
          throw new Error(errText);
        }

        stopProgress();
        progressBar.style.transition = 'width 0.3s ease';
        progressBar.style.width = '100%';
        loadingText.textContent = 'Done! Loading report…';

        const html = await res.text();
        setTimeout(() => {
          document.open();
          document.write(html);
          document.close();
        }, 300);

      } catch (err) {
        stopProgress();
        btn.style.display = 'flex';
        loading.style.display = 'none';
        progressBar.style.width = '0%';
        errorMsg.textContent = err.message || 'Something went wrong. Please try again.';
        errorMsg.style.display = 'block';
      }
    });
  </script>
</body>
</html>"""


@app.route("/", methods=["GET"])
def index():
    return LANDING_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/analyze", methods=["POST"])
def analyze():
    url = request.form.get("url", "").strip()
    if not url:
        return {"error": "No URL provided."}, 400

    yt_key = os.getenv("YOUTUBE_API_KEY") or os.getenv("YT_API_KEY") or os.getenv("YOUTUBE_KEY")
    if not yt_key:
        return {"error": "YouTube API key not found. Check your .env file."}, 500

    try:
        video_id = extract_video_id(url)
        if not video_id:
            return {"error": "Could not extract a video ID from that URL. Please paste a valid YouTube link."}, 400

        result = run_single(url, yt_key)
        log_run(
            video_id=extract_video_id(url),
            video_title=result.get("title", ""),
            channel=result.get("channel", ""),
            total_comments=result.get("total_comments", 0),
        )
        html_report = build_html_report(
            video_title=result["title"],
            channel=result["channel"],
            duration_seconds=result["duration"],
            total_comments=result["total_comments"],
            tier=result["tier"],
            buckets=result["buckets"],
            top_ts=result["top_ts"],
            clip_quotes=result["clip_quotes"],
            requests_data=result["requests_data"],
            sentiment_pct=result["sentiment_pct"],
            comments=result.get("comments", []),
            buckets_comments=result.get("buckets_comments", []),
            fan_vision=result.get("fan_vision", []),
            comments_at_ts=result.get("comments_at_ts", {}),
            quotable_lines=result.get("quotable_lines", []),
        )
        return Response(html_report, content_type="text/html; charset=utf-8")

    except Exception as exc:
        return {"error": str(exc)}, 500


@app.route("/stats")
def stats():
    total, today, recent = get_stats()
    rows = ""
    for ts, title, channel, comments in recent:
        t = ts[:16].replace("T", " ")
        rows += f"""<tr>
          <td>{t}</td>
          <td>{html.escape(title or "—")}</td>
          <td>{html.escape(channel or "—")}</td>
          <td>{comments or 0:,}</td>
        </tr>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fan Sentiment — Stats</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', -apple-system, sans-serif; background: #f2f2f7; color: #1c1c1e; padding: 48px; -webkit-font-smoothing: antialiased; }}
  h1 {{ font-size: 24px; font-weight: 700; letter-spacing: -0.3px; margin-bottom: 6px; }}
  .sub {{ font-size: 14px; color: #8e8e93; margin-bottom: 32px; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
  .card {{ background: #fff; border-radius: 16px; padding: 24px 28px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); min-width: 160px; }}
  .card-value {{ font-size: 36px; font-weight: 700; color: #ff6b3d; letter-spacing: -1px; }}
  .card-label {{ font-size: 11px; font-weight: 600; color: #8e8e93; text-transform: uppercase; letter-spacing: .06em; margin-top: 6px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 16px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
  th {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: #8e8e93; padding: 14px 20px; text-align: left; border-bottom: 1px solid rgba(0,0,0,0.06); }}
  td {{ font-size: 13px; padding: 14px 20px; border-bottom: 1px solid rgba(0,0,0,0.04); color: #3a3a3c; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #fafafa; }}
</style>
</head>
<body>
  <h1>Usage Stats</h1>
  <p class="sub">Fan Sentiment — all time activity</p>
  <div class="cards">
    <div class="card"><div class="card-value">{total:,}</div><div class="card-label">Total analyses</div></div>
    <div class="card"><div class="card-value">{today:,}</div><div class="card-label">Today</div></div>
  </div>
  <table>
    <thead><tr><th>Time (UTC)</th><th>Video</th><th>Channel</th><th>Comments</th></tr></thead>
    <tbody>{rows if rows else '<tr><td colspan="4" style="color:#8e8e93;text-align:center;padding:32px">No runs yet</td></tr>'}</tbody>
  </table>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
