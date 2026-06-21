"""
Daily snapshot fetcher — saves top 1000 coins from CoinGecko to data/YYYY-MM-DD.json
Run manually or via scheduled task at 06:00 CET.
"""

import json
import time
import datetime
import pathlib
import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "CryptoRankTracker/1.0",
}
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


def main():
    DATA_DIR.mkdir(exist_ok=True)
    fetched_at = datetime.datetime.now(datetime.timezone.utc)
    date_str = fetched_at.strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"{date_str}.json"

    print(f"Fetching top 1000 coins at {fetched_at.isoformat()} ...")
    coins = fetch_top1000()

    snapshot = {
        "fetchedAt": fetched_at.isoformat(),
        "count": len(coins),
        "coins": coins,
    }
    out_path.write_text(json.dumps(snapshot, separators=(",", ":")))
    print(f"Saved {len(coins)} coins → {out_path}")


if __name__ == "__main__":
    main()
