#!/usr/bin/env python3
"""
merch.py, direct-to-fan store analysis for Cadence.

Reads a Shopify store's public /products.json feed. That is a documented JSON
endpoint, not HTML scraping, so it does not carry the fragility that killed the
TikTok scraper work.

Deliberately NOT measured: merch drop cadence. Verified on Golf Wang that
`published_at` clusters inside a 6-month window on a store that has run for
years, i.e. it tracks inventory republishing rather than original drop dates.
Reporting a cadence off that would be a number we cannot defend.

Answers four questions: what does it cost to buy in, what is the core price,
how deep is the catalog, and what are they actually selling.
"""

import re
import json
import sys
from statistics import median, mean

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

PAGE_LIMIT = 250
MAX_PAGES = 12          # 3000 products is far beyond any artist store
TIMEOUT = 12


class MerchError(Exception):
    pass


# ── Store URL handling ───────────────────────────────────────────────────────

def normalize_store_url(raw):
    """
    Accept anything a human would paste: bare domain, full product URL,
    collection page, trailing slash, http/https, with or without www.
    Returns a scheme+host origin.
    """
    if not raw or not raw.strip():
        raise MerchError("Paste a store link to include merch analysis.")

    s = raw.strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = s.split("/")[0].split("?")[0].strip().lower()
    # Strip www so golfwang.com and www.golfwang.com resolve to one cache key.
    s = re.sub(r"^www\.", "", s)

    if not s or "." not in s:
        raise MerchError(
            "That doesn't look like a store link. Paste the store's homepage "
            "URL, e.g. https://yourstore.com"
        )
    return f"https://{s}"


# ── Fetch ────────────────────────────────────────────────────────────────────

def fetch_products(store_url):
    """
    Page through /products.json. Shopify caps this at 250 per page and stops
    returning items past the end.
    """
    origin = normalize_store_url(store_url)
    products, page = [], 1

    while page <= MAX_PAGES:
        try:
            r = requests.get(
                f"{origin}/products.json",
                params={"limit": PAGE_LIMIT, "page": page},
                headers={"User-Agent": UA},
                timeout=TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise MerchError(f"Couldn't reach that store ({type(e).__name__}).")

        if r.status_code == 404:
            raise MerchError(
                "No public product feed found. This works with Shopify stores, "
                "other platforms (Bandcamp, Squarespace, Big Cartel) don't expose one."
            )
        if not r.ok:
            raise MerchError(f"Store returned {r.status_code}.")
        if "json" not in r.headers.get("content-type", ""):
            raise MerchError(
                "That store isn't a Shopify store, so there's no product feed to read."
            )

        try:
            batch = r.json().get("products", [])
        except ValueError:
            raise MerchError("Store returned an unreadable product feed.")

        if not batch:
            break
        products.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        page += 1

    if not products:
        raise MerchError("That store's product feed is empty.")

    return origin, products


# ── Categorization ───────────────────────────────────────────────────────────

# product_type is free text and wildly inconsistent across stores
# ("TEES" / "T-Shirt" / "Long-sleeve" / "All Over Prints"), so match keywords, # but check product_type FIRST, since when a store does label an item it is more
# authoritative than guessing from the title.
#
# Order matters. Specific categories are tested before generic ones: "Crew Socks"
# must reach Accessories before the Tops rule sees "crew", and Golf Wang tags
# blankets and skate decks as ACCESSORIES with titles matching nothing.
# Trailing `s?` throughout: stores label types in the plural ("TOPS", "HATS")
# and a missing plural silently dumps whole categories into Other.
CATEGORY_RULES = [
    ("Music",       r"vinyl|\blps?\b|\bcds?\b|cassette|\brecords?\b|\b7\"|\b12\"|\bmusic\b"),
    ("Footwear",    r"shoes?\b|sneaker|footwear|boots?\b|slides?\b|sandal"),
    ("Headwear",    r"\bhats?\b|\bcaps?\b|beanie|bucket|snapback|strapback|headwear|visor|skully"),
    ("Bottoms",     r"pants?\b|shorts?\b|sweatpant|trouser|jeans?\b|bottoms?\b|skirt"),
    ("Accessories", r"accessor|socks?\b|\bbags?\b|tote|sticker|keychain|\bpins?\b|poster|"
                    r"towel|glass|mug|lighter|card ?holder|wallet|belt|jewel|chain|patch|"
                    r"blanket|notebook|candle|fragrance|decks?\b|\bmats?\b|nalgene|bottle|"
                    r"picks?\b|freshener|match|statue|incense|umbrella|rug|pillow|scarf|glove"),
    ("Tops",        r"tee|t-?shirts?\b|hoodie|fleece|crew ?neck|sweat|long-?sleeve|jacket|"
                    r"jersey|\btops?\b|shirts?\b|vest|coat|pullover|cardigan|polo|button|"
                    r"outerwear"),
]


def categorize(product):
    """
    product_type first, then title. Tags are deliberately ignored: they are
    campaign labels, not categories. Golf Wang tags a jacket and a poster
    "MUSIC DROP", which put both in the Music bucket until tags were dropped.
    """
    ptype = product.get("product_type", "") or ""
    title = product.get("title", "") or ""

    for name, pattern in CATEGORY_RULES:
        if ptype and re.search(pattern, ptype, re.I):
            return name

    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, title, re.I):
            return name

    return "Other"


def product_price(product):
    """
    Lowest variant price = the price a store displays. Using every variant would
    count one tee five times just because it comes in five sizes.
    """
    prices = []
    for v in (product.get("variants") or []):
        raw = v.get("price")
        if raw in (None, ""):
            continue
        try:
            p = float(raw)
        except (TypeError, ValueError):
            continue
        # Free items (fonts, downloads, giveaways) would drag the entry price
        # to $0 and misrepresent what it costs to buy in.
        if p > 0:
            prices.append(p)
    return min(prices) if prices else None


# ── Analysis ─────────────────────────────────────────────────────────────────

PRICE_BANDS = [("Under $30", 0, 30), ("$30–60", 30, 60),
               ("$60–100", 60, 100), ("$100+", 100, float("inf"))]


def analyze_store(store_url):
    origin, raw = fetch_products(store_url)

    priced, free_count = [], 0
    for p in raw:
        price = product_price(p)
        if price is None:
            free_count += 1
            continue
        priced.append({
            "title": p.get("title", ""),
            "price": price,
            "category": categorize(p),
            "handle": p.get("handle", ""),
            "url": f'{origin}/products/{p.get("handle","")}',
            "image": ((p.get("images") or [{}])[0] or {}).get("src", ""),
        })

    if not priced:
        raise MerchError("That store has no priced products to analyze.")

    prices = sorted(pr["price"] for pr in priced)

    categories = {}
    for pr in priced:
        categories[pr["category"]] = categories.get(pr["category"], 0) + 1
    categories = dict(sorted(categories.items(), key=lambda kv: -kv[1]))

    bands = {}
    for label, lo, hi in PRICE_BANDS:
        bands[label] = sum(1 for p in prices if lo <= p < hi)

    music_items = [pr for pr in priced if pr["category"] == "Music"]

    return {
        "store_url": origin,
        "store_name": origin.replace("https://", "").replace(".myshopify.com", ""),
        "product_count": len(priced),
        "free_or_unpriced": free_count,
        "entry_price": prices[0],
        "average_price": mean(prices),
        "median_price": median(prices),
        "top_price": prices[-1],
        "price_bands": bands,
        "categories": categories,
        "category_count": len(categories),
        "sells_physical_music": bool(music_items),
        "music_item_count": len(music_items),
        "cheapest": min(priced, key=lambda p: p["price"]),
        "most_expensive": max(priced, key=lambda p: p["price"]),
        "products": sorted(priced, key=lambda p: -p["price"]),
        "read": _plain_read(len(priced), prices, categories, bool(music_items)),
    }


def _plain_read(count, prices, categories, has_music):
    """One plain-English sentence. The chart is evidence; this is the product."""
    core = mean(prices)
    depth = "a tight capsule" if count <= 10 else (
        "a focused catalog" if count <= 40 else
        "a deep catalog" if count <= 120 else "a full retail catalog")
    top_cat = next(iter(categories), "merch")
    music = " They sell physical music direct to fans." if has_music else \
            " No physical music, apparel only."
    return (f"{depth.capitalize()} of {count} products, built around {top_cat.lower()}, "
            f"averaging ${core:,.0f} per item "
            f"(entry ${prices[0]:,.0f}, top ${prices[-1]:,.0f}).{music}")


def compare_stores(a, b):
    """Side-by-side positioning. Returns per-metric winner-agnostic diffs."""
    def pct(x, y):
        if not y:
            return None
        return round((x - y) / y * 100)

    return {
        "a": a, "b": b,
        "avg_price_gap_pct": pct(a["average_price"], b["average_price"]),
        "depth_gap": a["product_count"] - b["product_count"],
        "entry_gap": round(a["entry_price"] - b["entry_price"], 2),
        "read": (
            f'{a["store_name"]} averages ${a["average_price"]:,.0f} per item across '
            f'{a["product_count"]} products; {b["store_name"]} sits at '
            f'${b["average_price"]:,.0f} across {b["product_count"]}.'
        ),
    }


if __name__ == "__main__":
    urls = sys.argv[1:] or ["https://golfwang.com", "https://kaicash.myshopify.com"]
    results = []
    for u in urls:
        try:
            r = analyze_store(u)
            results.append(r)
            print(f'\n=== {r["store_name"].upper()} ===')
            print(f'  {r["read"]}\n')
            print(f'  products      : {r["product_count"]}  (excluded {r["free_or_unpriced"]} free/unpriced)')
            print(f'  price ladder  : entry ${r["entry_price"]:,.0f} | avg ${r["average_price"]:,.0f} | top ${r["top_price"]:,.0f}')
            print(f'  categories    : {r["categories"]}')
            print(f'  price bands   : {r["price_bands"]}')
            print(f'  physical music: {"yes (" + str(r["music_item_count"]) + ")" if r["sells_physical_music"] else "no"}')
            print(f'  cheapest      : {r["cheapest"]["title"][:44]} (${r["cheapest"]["price"]:,.0f})')
            print(f'  priciest      : {r["most_expensive"]["title"][:44]} (${r["most_expensive"]["price"]:,.0f})')
        except MerchError as e:
            print(f'\n=== {u} ===\n  ERROR: {e}')

    if len(results) == 2:
        c = compare_stores(*results)
        print(f'\n=== POSITIONING ===\n  {c["read"]}')
        print(f'  avg price gap: {c["avg_price_gap_pct"]:+}%   catalog depth gap: {c["depth_gap"]:+}')






# ── Rendering (Hey Mike brand system) ────────────────────────────────────────

CAT_COLORS = {
    "Tops": "#2f76dd", "Accessories": "#7aa5e8", "Music": "#1c3d7a",
    "Headwear": "#4d8ae4", "Bottoms": "#a8c4f0", "Footwear": "#c9dbf7",
    "Other": "#d8d8dd",
}
CAT_ORDER = ["Tops", "Bottoms", "Headwear", "Footwear", "Accessories", "Music", "Other"]
ROW_ITEMS = 6          # "top of the catalog" is a single row, never a wall


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _thumb(url, w=500):
    if not url:
        return ""
    return f'{url}{"&" if "?" in url else "?"}width={w}'


def _ladder_picks(store):
    prods = sorted(store["products"], key=lambda p: p["price"])
    mid = min(prods, key=lambda p: abs(p["price"] - store["median_price"]))
    return [("Cheapest", prods[0]), ("Typical", mid), ("Most expensive", prods[-1])]


def _product_card(p, label=None):
    tag = f'<span class="mk-tag">{_esc(label)}</span>' if label else ""
    img = _thumb(p.get("image"))
    art = (f'<img src="{_esc(img)}" alt="{_esc(p["title"])}" loading="lazy">'
           if img else '<div class="mk-noimg"></div>')
    return f'''<a class="mk-prod" href="{_esc(p["url"])}" target="_blank" rel="noopener">
      <div class="mk-prod-img">{art}{tag}</div>
      <div class="mk-prod-t">{_esc(p["title"][:52])}</div>
      <div class="mk-prod-m"><span class="mk-prod-p">${p["price"]:,.0f}</span>
        <span class="mk-prod-c">{_esc(p["category"])}</span></div>
    </a>'''


def _price_row(store, size="lg"):
    """Entry / Average / Top. The headline numbers, given real weight."""
    cls = "mk-prices" + (" mk-prices-sm" if size == "sm" else "")
    return f'''<div class="{cls}">
      <div><b>${store["entry_price"]:,.0f}</b><span>Entry</span></div>
      <div><b>${store["average_price"]:,.0f}</b><span>Average</span></div>
      <div><b>${store["top_price"]:,.0f}</b><span>Top</span></div>
    </div>'''


def _cat_rows(store, other=None):
    """Category counts. When a second store is given, show them side by side."""
    keys = [c for c in CAT_ORDER if c in store["categories"]
            or (other and c in other["categories"])]
    rows = ""
    for c in keys:
        n = store["categories"].get(c, 0)
        rows += (f'<div class="mk-catrow"><i style="background:{CAT_COLORS.get(c,"#d8d8dd")}"></i>'
                 f'<span class="mk-catname">{_esc(c)}</span>'
                 f'<span class="mk-catn">{n}</span>')
        if other is not None:
            rows += f'<span class="mk-catn mk-catn-b">{other["categories"].get(c, 0)}</span>'
        rows += "</div>"
    return rows


def render_summary(store_a, store_b=None, compare=None):
    """
    Head-to-head summary. This is the whole page in one card: who sells what,
    how much of it, and at what price.
    """
    if not store_b:
        return f'''
    <div class="card">
      <div class="card-title">Store Summary</div>
      <div class="mk-sumname">{_esc(store_a["store_name"])}</div>
      <p class="mk-read mk-lead">{_positioning_read(store_a)}</p>
      <p class="mk-read mk-facts">{_esc(store_a["read"])}</p>
      <div class="mk-sub">What they sell</div>
      <div class="mk-cattable">{_cat_rows(store_a)}</div>
    </div>'''

    gap = (compare or {}).get("avg_price_gap_pct")
    direction = "higher" if (gap or 0) > 0 else "lower"
    verdict = ""
    if gap is not None:
        verdict = (f'<p class="mk-verdict"><b>{_esc(store_a["store_name"])}</b> averages '
                   f'<b>{abs(gap)}% {direction}</b> per item and carries '
                   f'<b>{abs(compare["depth_gap"])} {"more" if compare["depth_gap"]>0 else "fewer"}</b> '
                   f'products than <b>{_esc(store_b["store_name"])}</b>.</p>')

    return f'''
    <div class="card">
      <div class="card-title">Head to Head</div>
      {verdict}
      <div class="mk-h2h">
        <div class="mk-h2h-col">
          <div class="mk-sumname">{_esc(store_a["store_name"])}</div>
          <div class="mk-sumsub">{store_a["product_count"]} products ·
            {store_a["music_item_count"]} music</div>
          {_price_row(store_a)}
        </div>
        <div class="mk-h2h-col">
          <div class="mk-sumname">{_esc(store_b["store_name"])}</div>
          <div class="mk-sumsub">{store_b["product_count"]} products ·
            {store_b["music_item_count"]} music</div>
          {_price_row(store_b)}
        </div>
      </div>

      <p class="mk-read mk-lead">{_positioning_read(store_a)}</p>
      <p class="mk-read mk-lead">{_positioning_read(store_b)}</p>

      <div class="mk-sub">What each store sells</div>
      <div class="mk-cattable mk-cattable-2">
        <div class="mk-catrow mk-cathead"><i></i><span class="mk-catname"></span>
          <span class="mk-catn">{_esc(store_a["store_name"][:12])}</span>
          <span class="mk-catn mk-catn-b">{_esc(store_b["store_name"][:12])}</span></div>
        {_cat_rows(store_a, store_b)}
      </div>
    </div>'''


def _store_block(store):
    ladder = "".join(_product_card(p, label) for label, p in _ladder_picks(store))
    row = "".join(_product_card(p) for p in store["products"][:ROW_ITEMS])
    band_max = max(store["price_bands"].values()) or 1
    bands = "".join(
        f'<div class="mk-band"><span class="mk-band-l">{_esc(label)}</span>'
        f'<span class="mk-band-t"><i style="width:{n/band_max*100:.0f}%"></i></span>'
        f'<span class="mk-band-n">{n}</span></div>'
        for label, n in store["price_bands"].items()
    )
    return f'''
    <div class="card">
      <div class="mk-store-head">
        <div>
          <div class="mk-store-name">{_esc(store["store_name"])}</div>
          <a class="mk-store-url" href="{_esc(store["store_url"])}" target="_blank"
             rel="noopener">{_esc(store["store_url"].replace("https://",""))}</a>
        </div>
      </div>
      {_price_row(store)}

      <div class="mk-sub">What each price actually buys</div>
      <div class="mk-grid mk-grid-3">{ladder}</div>

      <div class="mk-sub">Top of the catalog</div>
      <div class="mk-row">{row}</div>

      <div class="mk-sub">Price bands</div>
      {bands}
    </div>'''


MERCH_CSS = '''
    <style>
      .mk-read { font-size:15px; line-height:1.55; color:#3c3c43; margin:12px 0 4px; }
      .mk-verdict { font-size:16px; line-height:1.6; color:#1c1c1e; margin-bottom:16px; }
      .mk-verdict b { color:#2f76dd; }
      .mk-store-head { display:flex; justify-content:space-between; align-items:flex-start;
                       gap:20px; flex-wrap:wrap; }
      .mk-store-name { font-family:'Anton',sans-serif; text-transform:uppercase;
                       letter-spacing:.02em; font-size:26px; color:#1c1c1e; }
      .mk-store-url { font-size:13px; color:#2f76dd; text-decoration:none; }
      .mk-store-url:hover { text-decoration:underline; }

      /* Headline prices: full width, large, never tucked in a corner. */
      .mk-prices { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:16px 0 4px; }
      .mk-prices > div { background:#f9f9fb; border-radius:14px; padding:18px 10px; text-align:center; }
      .mk-prices b { display:block; font-family:'Anton',sans-serif; font-size:40px;
                     font-weight:400; color:#2f76dd; letter-spacing:.02em; line-height:1; }
      .mk-prices span { display:block; margin-top:6px; font-family:'Anton',sans-serif;
                        text-transform:uppercase; font-size:11px; letter-spacing:.1em; color:#8e8e93; }
      .mk-prices-sm b { font-size:30px; }

      .mk-h2h { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
      .mk-sumname { font-family:'Anton',sans-serif; text-transform:uppercase;
                    letter-spacing:.02em; font-size:22px; color:#1c1c1e; }
      .mk-sumsub { font-size:13px; color:#8e8e93; margin-top:2px; }

      .mk-cattable { margin-top:4px; }
      .mk-catrow { display:flex; align-items:center; gap:10px; padding:7px 0;
                   border-bottom:1px solid #f2f2f7; }
      .mk-catrow i { width:10px; height:10px; border-radius:3px; flex-shrink:0; }
      .mk-catname { flex:1; font-size:14px; color:#3c3c43; }
      .mk-catn { width:96px; text-align:right; font-family:'Anton',sans-serif;
                 font-size:18px; color:#1c1c1e; }
      .mk-catn-b { color:#2f76dd; }
      .mk-cathead { border-bottom:1px solid #e5e5ea; padding-bottom:4px; }
      .mk-cathead .mk-catn { font-family:'Anton',sans-serif; font-size:10px;
                             letter-spacing:.08em; text-transform:uppercase; color:#8e8e93; }

      .mk-sub { font-family:'Anton',sans-serif; text-transform:uppercase; letter-spacing:.06em;
                font-size:12px; color:#8e8e93; margin:22px 0 10px; }
      .mk-grid { display:grid; gap:14px; }
      .mk-grid-3 { grid-template-columns:repeat(3,1fr); }
      /* Single row, horizontally scrollable rather than wrapping into a wall. */
      .mk-row { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(140px,1fr);
                gap:14px; overflow-x:auto; padding-bottom:4px; }
      .mk-prod { text-decoration:none; color:inherit; display:block; }
      .mk-prod-img { position:relative; aspect-ratio:1/1; background:#f2f2f7; border-radius:12px;
                     overflow:hidden; }
      .mk-prod-img img { width:100%; height:100%; object-fit:cover; display:block;
                         transition:transform .25s ease; }
      .mk-prod:hover .mk-prod-img img { transform:scale(1.05); }
      .mk-noimg { width:100%; height:100%; background:
                  repeating-linear-gradient(45deg,#f2f2f7,#f2f2f7 8px,#eaeaef 8px,#eaeaef 16px); }
      .mk-tag { position:absolute; top:8px; left:8px; background:#2f76dd; color:#fff;
                font-family:'Anton',sans-serif; text-transform:uppercase; font-size:10px;
                letter-spacing:.06em; padding:3px 8px; border-radius:999px; }
      .mk-prod-t { font-size:13px; line-height:1.35; margin-top:8px; color:#1c1c1e;
                   min-height:35px; display:-webkit-box; -webkit-line-clamp:2;
                   -webkit-box-orient:vertical; overflow:hidden; }
      .mk-prod-m { display:flex; justify-content:space-between; align-items:baseline;
                   margin-top:3px; gap:6px; }
      .mk-prod-p { font-family:'Anton',sans-serif; font-size:17px; color:#2f76dd; }
      .mk-prod-c { font-size:11px; color:#8e8e93; }

      .mk-band { display:flex; align-items:center; gap:12px; margin-bottom:8px; }
      .mk-band-l { width:90px; font-size:13px; color:#6c6c70; }
      .mk-band-t { flex:1; background:#f2f2f7; border-radius:999px; height:10px; overflow:hidden; }
      .mk-band-t i { display:block; height:100%; background:#2f76dd; border-radius:999px; }
      .mk-band-n { width:34px; text-align:right; font-size:13px; color:#1c1c1e; font-weight:600; }

      /* Interpretation paragraph, sits inside the summary card. */
      .mk-lead { font-size:16px; line-height:1.6; color:#1c1c1e; margin-top:12px; }
      .mk-facts { font-size:14px; line-height:1.55; color:#6c6c70; margin-top:10px; }

      /* Head-to-head positioning figures. */
      .mk-pos { display:flex; gap:36px; margin-top:12px; flex-wrap:wrap; }
      .mk-pos-n { font-family:'Anton',sans-serif; font-size:30px; color:#2f76dd; display:block; }
      .mk-pos-l { font-size:12px; color:#8e8e93; text-transform:uppercase; letter-spacing:.06em; }

      /* Glossary. Terms must sit on their own line or they run into the text. */
      .mk-gloss { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
                  gap:18px; }
      .mk-gloss > div { }
      .mk-gloss b { display:block; font-family:'Anton',sans-serif; text-transform:uppercase;
                    letter-spacing:.02em; font-size:13px; color:#1c1c1e; margin-bottom:4px; }
      .mk-gloss span { display:block; font-size:13.5px; line-height:1.55; color:#3c3c43; }
      .mk-note { font-size:12.5px; color:#8e8e93; line-height:1.6; margin-top:18px;
                 border-top:1px solid #f2f2f7; padding-top:14px; }

      @media (max-width:700px) {
        .mk-h2h { grid-template-columns:1fr; gap:26px; }
        .mk-prices b { font-size:30px; }
        .mk-catn { width:70px; font-size:16px; }
        .mk-grid-3 { grid-template-columns:1fr; }
      }
    </style>'''


def _positioning_read(s):
    """Interpret this store's own numbers rather than just listing them."""
    top_cat = next(iter(s["categories"]), "merch")
    spread = (s["top_price"] / s["entry_price"]) if s["entry_price"] else 0
    if s["product_count"] <= 10:
        depth = ("a tight capsule, which keeps production risk low but gives a fan "
                 "very few reasons to come back between drops")
    elif s["product_count"] <= 40:
        depth = ("a focused range, wide enough to give fans a choice without carrying "
                 "the overhead of a full retail catalog")
    else:
        depth = ("a deep catalog that behaves like a standalone retail business, not "
                 "a merch table")
    if spread >= 8:
        ladder = (f'The ${s["entry_price"]:,.0f} entry point and ${s["top_price"]:,.0f} '
                  "ceiling mean there is something here for a casual fan and something "
                  "for a collector, which is how a store maximizes revenue per visitor.")
    elif spread >= 3:
        ladder = (f'Prices run ${s["entry_price"]:,.0f} to ${s["top_price"]:,.0f}, a normal '
                  "spread that covers most fan budgets.")
    else:
        ladder = (f'Almost everything sits near ${s["average_price"]:,.0f}, so there is no '
                  "cheap way in and no premium item to trade up to.")
    music = ("They also sell physical music direct, which captures full margin instead of "
             "a streaming royalty." if s["sells_physical_music"] else
             "There is no physical music here, so the store is pure apparel and does not "
             "capture direct music revenue.")
    return (f'{_esc(s["store_name"])} runs {depth}, weighted toward '
            f'{top_cat.lower()}. {ladder} {music}')


MERCH_GLOSSARY = """
    <div class="card">
      <div class="card-title">How to read this</div>
      <div class="mk-gloss">
        <div><b>Entry price</b><span>The cheapest item in the store. This is what it costs a
          fan to buy in for the first time.</span></div>
        <div><b>Average price</b><span>The mean price across every product. Products are counted
          once, not once per size, so a tee in five sizes counts as one item.</span></div>
        <div><b>Top price</b><span>The most expensive item. The distance between entry and top
          shows whether there is a ladder for fans to climb.</span></div>
        <div><b>Catalog depth</b><span>How many products are on sale. Depth drives repeat
          visits; a small catalog relies on drops instead.</span></div>
        <div><b>Category mix</b><span>What they actually sell, grouped from the store's own
          product labels. Heavy apparel versus physical music tells you whether the store is a
          merch business or a music business.</span></div>
        <div><b>Price bands</b><span>How products spread across price tiers, which shows whether
          the range is genuinely accessible or clustered at one price.</span></div>
      </div>
      <p class="mk-note">Free items are excluded from all pricing figures so a giveaway does not
      drag the entry price to zero. Figures come from the store's public product feed and reflect
      list prices, not what actually sold.</p>
    </div>"""


def render_merch_tab(store_a, store_b=None, compare=None):
    """Summary first (it explains the whole page), then each store in detail."""
    # The interpretation now lives inside the summary card itself, directly under
    # the header, rather than in a separate card below it.
    out = MERCH_CSS + render_summary(store_a, store_b, compare) + _store_block(store_a)
    if store_b:
        out += _store_block(store_b)
    return out + MERCH_GLOSSARY
