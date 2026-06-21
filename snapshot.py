"""
Daily snapshot fetcher — saves top 1000 coins from CoinGecko to PostgreSQL.
Run manually or via scheduled task at 06:00 CET.

Requires DATABASE_URL env var (set automatically by Railway Postgres addon).
Table is created on first run if it doesn't exist.
"""

import json
import os
import time
import datetime
import pathlib
import requests
import psycopg2
from psycopg2.extras import Json

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "CryptoRankTracker/1.0",
}
# Fallback: also write JSON file locally when DATABASE_URL is not set
DATA_DIR = pathlib.Path(__file__).parent / "data"


def fetch_top1000():
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
            "volume24h": coin.get("total_volume"),
            "image": coin.get("image", ""),
        })
    return result


def save_to_postgres(fetched_at, coins):
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS snapshots (
                        id         SERIAL PRIMARY KEY,
                        fetched_at TIMESTAMPTZ NOT NULL,
                        date       DATE        NOT NULL UNIQUE,
                        coin_count INTEGER     NOT NULL,
                        coins      JSONB       NOT NULL
                    )
                """)
                cur.execute("""
                    INSERT INTO snapshots (fetched_at, date, coin_count, coins)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (date) DO UPDATE
                        SET fetched_at = EXCLUDED.fetched_at,
                            coin_count = EXCLUDED.coin_count,
                            coins      = EXCLUDED.coins
                """, (fetched_at, fetched_at.date(), len(coins), Json(coins)))
    finally:
        conn.close()


def save_to_file(fetched_at, coins):
    DATA_DIR.mkdir(exist_ok=True)
    date_str = fetched_at.strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"{date_str}.json"
    snapshot = {"fetchedAt": fetched_at.isoformat(), "count": len(coins), "coins": coins}
    out_path.write_text(json.dumps(snapshot, separators=(",", ":")))
    print(f"Saved {len(coins)} coins → {out_path}")


def main():
    fetched_at = datetime.datetime.now(datetime.timezone.utc)
    print(f"Fetching top 1000 coins at {fetched_at.isoformat()} ...")
    coins = fetch_top1000()
    print(f"Fetched {len(coins)} coins.")

    if os.environ.get("DATABASE_URL"):
        save_to_postgres(fetched_at, coins)
        print(f"Saved to PostgreSQL (date={fetched_at.date()}, rows=1).")
    else:
        save_to_file(fetched_at, coins)


if __name__ == "__main__":
    main()
