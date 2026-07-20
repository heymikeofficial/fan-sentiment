import os
import sqlite3
import hashlib
from datetime import datetime, date, timedelta
from flask import Flask, request, render_template_string
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.expanduser("~"), "Desktop", ".env"))

from channel_compare import run_comparison

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compare_usage.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comparisons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                channel_a TEXT,
                channel_b TEXT,
                ip TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comparison_cache (
                cache_key TEXT PRIMARY KEY,
                html TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

def make_cache_key(url_a, url_b):
    def norm(u):
        return u.strip().lower().rstrip("/")
    pair = sorted([norm(url_a), norm(url_b)])
    return hashlib.sha256("|".join(pair).encode()).hexdigest()

CACHE_TTL_HOURS = 12

def get_cached(cache_key):
    """Return cached html if a row exists created within the last 12 hours, else None."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT html, created_at FROM comparison_cache WHERE cache_key = ?",
            (cache_key,)
        ).fetchone()
    if not row:
        return None
    html, created_at = row
    try:
        created = datetime.fromisoformat(created_at)
    except Exception:
        return None
    if datetime.utcnow() - created > timedelta(hours=CACHE_TTL_HOURS):
        return None
    return html

def set_cached(cache_key, html):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO comparison_cache (cache_key, html, created_at) VALUES (?,?,?)",
            (cache_key, html, datetime.utcnow().isoformat())
        )

def log_comparison(channel_a, channel_b, ip):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO comparisons (ts, channel_a, channel_b, ip) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), channel_a, channel_b, ip)
        )

def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0]
        today_str = date.today().isoformat()
        today = conn.execute(
            "SELECT COUNT(*) FROM comparisons WHERE ts LIKE ?", (today_str + "%",)
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT ts, channel_a, channel_b, ip FROM comparisons ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return total, today, recent

def check_rate_limit(ip):
    """Check if IP has exceeded rate limits. Returns (allowed, remaining, reset_time)"""
    with sqlite3.connect(DB_PATH) as conn:
        today_str = date.today().isoformat()
        
        # Global limit: 50 per day
        global_count = conn.execute(
            "SELECT COUNT(*) FROM comparisons WHERE ts LIKE ?", (today_str + "%",)
        ).fetchone()[0]
        
        # Per-IP limit: 3 per day
        ip_count = conn.execute(
            "SELECT COUNT(*) FROM comparisons WHERE ip = ? AND ts LIKE ?",
            (ip, today_str + "%")
        ).fetchone()[0]
        
        global_allowed = global_count < 50
        ip_allowed = ip_count < 3
        
        return (global_allowed and ip_allowed, 3 - ip_count, tomorrow_midnight())

def tomorrow_midnight():
    """Return seconds until midnight"""
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day) + __import__('datetime').timedelta(days=1)
    return int((midnight - now).total_seconds())

init_db()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>YouTube Channel Comparison</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f7f8fa;
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
      max-width: 600px;
      width: 100%;
      margin: 24px;
    }

    .header {
      display: flex;
      align-items: center;
      gap: 10px;
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 28px;
      font-weight: 400;
      color: #1c1c1e;
      margin-bottom: 8px;
    }

    .powered-by {
      position: fixed;
      bottom: 16px;
      right: 20px;
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 1px;
      font-size: 33px;
      color: #2f76dd;
      opacity: 0.85;
      z-index: 50;
    }

    @media (max-width: 700px) {
      .powered-by {
        position: static;
        text-align: center;
        margin: 24px 0;
      }
    }

    .subtitle {
      font-size: 15px;
      color: #6c6c70;
      line-height: 1.6;
      margin: 12px 0 28px;
    }

    .input-group {
      margin-bottom: 14px;
    }

    .input-label {
      display: block;
      font-size: 13px;
      font-weight: 600;
      color: #1c1c1e;
      margin-bottom: 6px;
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
      border-color: #2f76dd;
      box-shadow: 0 0 0 3px rgba(47,118,221,0.15);
    }

    .btn-compare {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      height: 48px;
      margin-top: 14px;
      background: #2f76dd;
      color: #fff;
      border: none;
      border-radius: 12px;
      font-weight: 600;
      font-size: 15px;
      font-family: inherit;
      cursor: pointer;
      transition: background 0.15s;
    }

    .btn-compare:hover {
      background: #2560b8;
    }

    .btn-compare:disabled {
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
      color: #2f76dd;
      font-size: 14px;
      font-weight: 500;
    }

    .spinner {
      width: 16px;
      height: 16px;
      border: 2px solid rgba(47,118,221,0.25);
      border-top-color: #2f76dd;
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
      background: rgba(47,118,221,0.15);
      border-radius: 99px;
      overflow: hidden;
    }

    .progress-bar {
      height: 100%;
      width: 0%;
      background: #2f76dd;
      border-radius: 99px;
      transition: width 0.6s ease;
    }

    .progress-steps {
      font-size: 12px;
      color: #8e8e93;
    }

    .note {
      font-size: 13px;
      color: #8e8e93;
      text-align: center;
      margin-top: 16px;
    }

    .error-msg {
      display: none;
      margin-top: 12px;
      padding: 12px;
      background: #fff3cd;
      border-radius: 8px;
      font-size: 14px;
      color: #856404;
      line-height: 1.5;
    }

    .error-msg.danger {
      background: #f8d7da;
      color: #721c24;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <svg width="28" height="20" viewBox="0 0 18 13" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M17.6 2.03C17.4 1.29 16.83 0.71 16.1 0.5C14.7 0.13 9 0.13 9 0.13C9 0.13 3.3 0.13 1.9 0.5C1.17 0.71 0.6 1.29 0.4 2.03C0 3.44 0 6.38 0 6.38C0 6.38 0 9.32 0.4 10.73C0.6 11.47 1.17 12.03 1.9 12.24C3.3 12.61 9 12.61 9 12.61C9 12.61 14.7 12.61 16.1 12.24C16.83 12.03 17.4 11.47 17.6 10.73C18 9.32 18 6.38 18 6.38C18 6.38 18 3.44 17.6 2.03Z" fill="#2F76DD"/>
        <path d="M7.18 9.07L11.88 6.38L7.18 3.69V9.07Z" fill="white"/>
      </svg>
      Channel Comparison
    </div>
    <p class="subtitle">Compare any two YouTube channels side by side — subscribers, engagement, content strategy, and audience sentiment.</p>

    <form id="compareForm">
      <div class="input-group">
        <label class="input-label">Channel A</label>
        <input
          class="input-url"
          type="url"
          id="urlA"
          name="channel_a"
          placeholder="https://www.youtube.com/@channel1"
          required
          autocomplete="off"
          spellcheck="false"
        />
      </div>

      <div class="input-group">
        <label class="input-label">Channel B</label>
        <input
          class="input-url"
          type="url"
          id="urlB"
          name="channel_b"
          placeholder="https://www.youtube.com/@channel2"
          required
          autocomplete="off"
          spellcheck="false"
        />
      </div>

      <button class="btn-compare" type="submit" id="compareBtn">Compare Channels</button>
      <div class="loading-wrap" id="loadingWrap">
        <div class="loading-label">
          <div class="spinner"></div>
          <span id="loadingText">Fetching channel data…</span>
        </div>
        <div class="progress-track">
          <div class="progress-bar" id="progressBar"></div>
        </div>
        <div class="progress-steps" id="progressSteps">Takes about 45–60 seconds</div>
      </div>
      <div class="error-msg" id="errorMsg"></div>
      <p class="note">Rate limit: 3 comparisons per day per IP, 50 per day globally.</p>
    </form>
  </div>

  <script>
    const form = document.getElementById('compareForm');
    const btn = document.getElementById('compareBtn');
    const loading = document.getElementById('loadingWrap');
    const errorMsg = document.getElementById('errorMsg');
    const progressBar = document.getElementById('progressBar');
    const loadingText = document.getElementById('loadingText');
    const progressSteps = document.getElementById('progressSteps');

    const steps = [
      { pct: 12, label: 'Fetching channel data…',      note: 'Pulling metadata for both channels' },
      { pct: 30, label: 'Analyzing recent videos…',    note: 'Scanning last 20 videos from each' },
      { pct: 50, label: 'Reading audience comments…',  note: 'Collecting top comments from latest video' },
      { pct: 70, label: 'Calculating metrics…',        note: 'Running engagement analysis' },
      { pct: 85, label: 'Running AI analysis…',        note: 'Claude is comparing content & sentiment' },
    ];

    let stepTimers = [];

    function startProgress() {
      const totalMs = 65000;
      steps.forEach((s) => {
        const t = setTimeout(() => {
          progressBar.style.width = s.pct + '%';
          loadingText.textContent = s.label;
          progressSteps.textContent = s.note;
        }, (s.pct / 95) * totalMs);
        stepTimers.push(t);
      });
    }

    function stopProgress() {
      stepTimers.forEach(clearTimeout);
      stepTimers = [];
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();

      const urlA = document.getElementById('urlA').value.trim();
      const urlB = document.getElementById('urlB').value.trim();
      
      if (!urlA || !urlB) return;

      btn.style.display = 'none';
      loading.style.display = 'flex';
      errorMsg.style.display = 'none';
      progressBar.style.width = '4%';
      startProgress();

      try {
        const formData = new FormData();
        formData.append('channel_a', urlA);
        formData.append('channel_b', urlB);

        const res = await fetch('/compare', { method: 'POST', body: formData });

        stopProgress();

        if (!res.ok) {
          let errText = 'Comparison failed.';
          try {
            const data = await res.json();
            errText = data.error || errText;
          } catch (e) {
            const text = await res.text();
            if (text.includes('rate limit')) {
              errText = 'Rate limit exceeded. Please try again later.';
            }
          }
          throw new Error(errText);
        }

        // Success: redirect to report
        const html = await res.text();
        document.open();
        document.write(html);
        document.close();

      } catch (err) {
        stopProgress();
        btn.style.display = 'flex';
        loading.style.display = 'none';
        errorMsg.style.display = 'block';
        errorMsg.classList.add('danger');
        errorMsg.textContent = 'Error: ' + (err.message || 'Comparison failed. Check URLs and try again.');
      }
    });
  </script>
  <div class="powered-by">Powered by Hey Mike</div>
</body>
</html>"""

STATS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Comparison Stats</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f7f8fa;
      min-height: 100vh;
      padding: 24px;
    }

    .container {
      max-width: 900px;
      margin: 0 auto;
    }

    .card {
      background: #fff;
      border-radius: 16px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.06);
      padding: 24px;
      margin-bottom: 24px;
    }

    h1 {
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      font-size: 28px;
      font-weight: 400;
      color: #1c1c1e;
      margin-bottom: 24px;
    }

    .powered-by {
      position: fixed;
      bottom: 16px;
      right: 20px;
      font-family: 'Anton', sans-serif;
      text-transform: uppercase;
      letter-spacing: 1px;
      font-size: 33px;
      color: #2f76dd;
      opacity: 0.85;
      z-index: 50;
    }

    @media (max-width: 700px) {
      .powered-by {
        position: static;
        text-align: center;
        margin: 24px 0;
      }
    }

    .stat {
      display: flex;
      justify-content: space-between;
      padding: 12px 0;
      border-bottom: 1px solid #e5e5ea;
    }

    .stat:last-child {
      border-bottom: none;
    }

    .stat-label {
      color: #6c6c70;
      font-size: 14px;
    }

    .stat-value {
      font-weight: 600;
      color: #1c1c1e;
      font-size: 14px;
    }

    .back-link {
      display: inline-block;
      color: #2f76dd;
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
      margin-bottom: 20px;
    }

    .back-link:hover {
      text-decoration: underline;
    }

    table {
      width: 100%;
      border-collapse: collapse;
    }

    thead th {
      text-align: left;
      padding: 12px;
      background: #f2f2f7;
      font-weight: 600;
      font-size: 13px;
      color: #1c1c1e;
      border-bottom: 1px solid #e5e5ea;
    }

    tbody td {
      padding: 12px;
      border-bottom: 1px solid #e5e5ea;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="container">
    <a href="/" class="back-link">← Back to Comparison Tool</a>

    <div class="card">
      <h1>Comparison Stats</h1>

      <div class="stat">
        <span class="stat-label">Total Comparisons</span>
        <span class="stat-value">{{ total }}</span>
      </div>
      <div class="stat">
        <span class="stat-label">Today's Comparisons</span>
        <span class="stat-value">{{ today }}</span>
      </div>
    </div>

    <div class="card">
      <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 12px;">Recent Activity</h3>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Channel A</th>
            <th>Channel B</th>
            <th>IP</th>
          </tr>
        </thead>
        <tbody>
          {% for row in recent %}
          <tr>
            <td>{{ row[0] }}</td>
            <td>{{ row[1][:40] }}</td>
            <td>{{ row[2][:40] }}</td>
            <td>{{ row[3] }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  <div class="powered-by">Powered by Hey Mike</div>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(LANDING_HTML)

@app.route("/compare", methods=["POST"])
def compare():
    channel_a = request.form.get("channel_a", "").strip()
    channel_b = request.form.get("channel_b", "").strip()
    
    if not channel_a or not channel_b:
        return {"error": "Both channel URLs are required"}, 400

    ip = request.remote_addr

    # Check cache FIRST — a hit is free and instant (no quota, no rate limit).
    cache_key = make_cache_key(channel_a, channel_b)
    cached_html = get_cached(cache_key)
    if cached_html is not None:
        return cached_html, 200, {"Content-Type": "text/html; charset=utf-8"}

    # Cache miss — enforce rate limit before spending quota.
    allowed, remaining, reset_time = check_rate_limit(ip)

    if not allowed:
        return {
            "error": f"Rate limit exceeded. You have {remaining} comparisons left today. Resets in {reset_time} seconds."
        }, 429

    try:
        # Run comparison
        html_report = run_comparison(channel_a, channel_b)

        # Store result in cache
        set_cached(cache_key, html_report)

        # Log the comparison
        log_comparison(channel_a, channel_b, ip)

        return html_report, 200, {"Content-Type": "text/html; charset=utf-8"}
    
    except ValueError as e:
        return {"error": f"Invalid channel URL: {str(e)}"}, 400
    except Exception as e:
        print(f"Error: {e}")
        return {"error": f"Comparison failed: {str(e)}"}, 500

@app.route("/stats")
def stats():
    total, today, recent = get_stats()
    html = render_template_string(STATS_HTML, total=total, today=today, recent=recent)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=False)
