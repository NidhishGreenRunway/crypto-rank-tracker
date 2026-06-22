"""
Crypto Rank Tracker — Flask Backend
Fetches top 1000 coins from CoinGecko (no API key required).
Caches results for 5 minutes to respect rate limits.
Persists daily snapshots to PostgreSQL when DATABASE_URL is set.
"""

import os
import time
import threading
import datetime
import requests
import psycopg2
from psycopg2.extras import Json
from flask import Flask, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory cache (CoinGecko responses)
# ---------------------------------------------------------------------------
_cache = {"data": None, "ts": 0}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # seconds

# In-memory snapshot store — fallback when DATABASE_URL is not set
_mem_snapshots = {}  # { "ISO-timestamp": [compact_coin, ...] }
_mem_lock = threading.Lock()

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "CryptoRankTracker/1.0",
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _db_url():
    return os.environ.get("DATABASE_URL")


def _get_conn():
    url = _db_url()
    if not url:
        return None
    return psycopg2.connect(url)


def _init_db():
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS snapshots (
                        id         SERIAL PRIMARY KEY,
                        fetched_at TIMESTAMPTZ NOT NULL,
                        date       DATE        NOT NULL UNIQUE,
                        coins      JSONB       NOT NULL
                    )
                """)
    finally:
        conn.close()


def _save_snapshot_db(fetched_at, compact_coins):
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO snapshots (fetched_at, date, coins)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (date) DO UPDATE
                        SET fetched_at = EXCLUDED.fetched_at,
                            coins      = EXCLUDED.coins
                """, (fetched_at, fetched_at.date(), Json(compact_coins)))
    finally:
        conn.close()


def _load_snapshots_db():
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT fetched_at, coins FROM snapshots ORDER BY fetched_at")
            rows = cur.fetchall()
        result = {}
        for fetched_at, coins in rows:
            result[fetched_at.isoformat()] = coins
        return result
    finally:
        conn.close()


def _clear_snapshots_db():
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM snapshots")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CoinGecko fetch
# ---------------------------------------------------------------------------
def fetch_top1000():
    """Fetch top 1000 coins by market cap from CoinGecko, 4 pages × 250."""
    coins = []
    for page in range(1, 5):
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{COINGECKO_BASE}/coins/markets",
                    params={
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": 250,
                        "page": page,
                        "sparkline": False,
                        "price_change_percentage": "24h",
                    },
                    headers=HEADERS,
                    timeout=20,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 30))
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                coins.extend(resp.json())
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    raise RuntimeError(f"CoinGecko error on page {page}: {exc}") from exc
                time.sleep(5)

        if page < 4:
            time.sleep(1.5)

    result = []
    for rank, coin in enumerate(coins, start=1):
        cg_id = coin.get("id", "")
        result.append({
            "rank": rank,
            "id": cg_id,
            "name": coin.get("name", ""),
            "symbol": (coin.get("symbol") or "").upper(),
            "price": coin.get("current_price"),
            "change24h": coin.get("price_change_percentage_24h"),
            "marketCap": coin.get("market_cap"),
            "image": coin.get("image", ""),
            "cgUrl": f"https://www.coingecko.com/en/coins/{cg_id}",
            "cmcUrl": f"https://coinmarketcap.com/currencies/{cg_id}/",
        })
    return result


def _compact(coins):
    """Minimal per-coin record for snapshot storage."""
    return [
        {"r": c["rank"], "i": c["id"], "n": c["name"], "s": c["symbol"],
         "cg": c["cgUrl"], "cmc": c["cmcUrl"], "img": c.get("image", "")}
        for c in coins
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/top1000")
def api_top1000():
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
            return jsonify({"cached": True, "fetchedAt": _cache["ts"], "coins": _cache["data"]})

    try:
        coins = fetch_top1000()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    fetched_at = datetime.datetime.now(datetime.timezone.utc)
    compact = _compact(coins)

    # Persist snapshot
    if _db_url():
        _save_snapshot_db(fetched_at, compact)
    else:
        with _mem_lock:
            _mem_snapshots[fetched_at.isoformat()] = compact

    with _cache_lock:
        _cache["data"] = coins
        _cache["ts"] = time.time()

    return jsonify({"cached": False, "fetchedAt": _cache["ts"], "coins": coins})


@app.route("/api/snapshots")
def api_snapshots():
    if _db_url():
        snapshots = _load_snapshots_db() or {}
    else:
        with _mem_lock:
            snapshots = dict(_mem_snapshots)
    return jsonify({"snapshots": snapshots, "count": len(snapshots)})


@app.route("/api/snapshots/clear", methods=["POST"])
def api_snapshots_clear():
    if _db_url():
        _clear_snapshots_db()
    else:
        with _mem_lock:
            _mem_snapshots.clear()
    return jsonify({"ok": True})


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    with _cache_lock:
        _cache["data"] = None
        _cache["ts"] = 0
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Scheduled daily snapshot — runs at 06:00 CET on Railway
# ---------------------------------------------------------------------------
def scheduled_snapshot():
    print(f"[scheduler] Running daily snapshot at {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    try:
        coins = fetch_top1000()
    except RuntimeError as exc:
        print(f"[scheduler] Fetch failed: {exc}")
        return

    fetched_at = datetime.datetime.now(datetime.timezone.utc)
    compact = _compact(coins)

    if _db_url():
        _save_snapshot_db(fetched_at, compact)
        print(f"[scheduler] Saved {len(coins)} coins to DB.")
    else:
        with _mem_lock:
            _mem_snapshots[fetched_at.isoformat()] = compact
        print(f"[scheduler] Saved {len(coins)} coins to memory (no DATABASE_URL).")

    # Warm the in-memory cache too
    with _cache_lock:
        _cache["data"] = coins
        _cache["ts"] = time.time()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
_init_db()

_scheduler = BackgroundScheduler()
_scheduler.add_job(
    scheduled_snapshot,
    CronTrigger(hour=6, minute=0, timezone=pytz.timezone("Europe/Paris")),
)
_scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"Starting Crypto Rank Tracker on http://localhost:{port}")
    app.run(debug=False, port=port, threaded=True)
