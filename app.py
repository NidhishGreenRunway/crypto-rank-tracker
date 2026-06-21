"""
Crypto Rank Tracker — Flask Backend
Fetches top 1000 coins from CoinGecko (no API key required).
Caches results for 5 minutes to respect rate limits.
"""

import os
import time
import threading
import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_cache = {"data": None, "ts": 0}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # seconds

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "CryptoRankTracker/1.0",
}


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
                    # Rate limited — wait and retry
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

        # Be kind to the free tier — pause between pages
        if page < 4:
            time.sleep(1.5)

    # Build minimal, flat objects for the frontend
    result = []
    for rank, coin in enumerate(coins, start=1):
        cg_id = coin.get("id", "")
        result.append(
            {
                "rank": rank,
                "id": cg_id,
                "name": coin.get("name", ""),
                "symbol": (coin.get("symbol") or "").upper(),
                "price": coin.get("current_price"),
                "change24h": coin.get("price_change_percentage_24h"),
                "marketCap": coin.get("market_cap"),
                "image": coin.get("image", ""),
                "cgUrl": f"https://www.coingecko.com/en/coins/{cg_id}",
                # CoinGecko IDs usually match CMC slugs; works for the majority
                "cmcUrl": f"https://coinmarketcap.com/currencies/{cg_id}/",
            }
        )
    return result


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

    with _cache_lock:
        _cache["data"] = coins
        _cache["ts"] = time.time()

    return jsonify({"cached": False, "fetchedAt": _cache["ts"], "coins": coins})


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    with _cache_lock:
        _cache["data"] = None
        _cache["ts"] = 0
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"Starting Crypto Rank Tracker on http://localhost:{port}")
    app.run(debug=False, port=port, threaded=True)
