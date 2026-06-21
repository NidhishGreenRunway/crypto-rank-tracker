# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server (http://localhost:5000)
python app.py

# Or use the convenience script
./launch.sh
```

There are no tests or linters configured yet.

## Architecture

This is a two-file Flask app. All logic lives in `app.py` (backend) and `templates/index.html` (frontend SPA). There is no build step, no JS bundler, no database.

**Data flow:**
1. Browser calls `GET /api/top1000` → Flask fetches CoinGecko `/coins/markets` across 4 pages (250 coins each, 1.5 s pause between pages to respect the free-tier rate limit) → returns a flat JSON array of 1000 coin objects.
2. On success, the frontend immediately serialises a compact snapshot into `localStorage` under the key `crt_snapshots_v2`. Each coin is stored in a minified form `{r, i, n, s, cg, cmc, img}` to stay well under the ~5 MB browser limit (~80 KB per snapshot × 400 snapshots max).
3. All analytics (breakout detection, biggest movers) run entirely in the browser against `localStorage` snapshots — there is no server-side persistence.

**Backend (`app.py`):**
- Single in-process dict `_cache` (protected by `threading.Lock`) holds the last fetched result for `CACHE_TTL = 300` seconds. This prevents hammering CoinGecko on page reload.
- `fetch_top1000()` retries each page up to 3 times, honouring `Retry-After` on HTTP 429.
- Coin rank is the array position (1-indexed), not CoinGecko's `market_cap_rank` field, so it is always contiguous 1–1000.
- CMC URLs are constructed from CoinGecko IDs (`coinmarketcap.com/currencies/{cg_id}/`), which matches for the vast majority of coins but may be wrong for a small number.

**Frontend (`templates/index.html`):**
The entire frontend is a single self-contained HTML file with inline CSS and vanilla JS — no framework, no external dependencies.

Key JS state variables:
- `currentCoins` — the most recently fetched array (full objects with price/change fields).
- `allSnapshots` — the in-memory mirror of `localStorage`, keyed by ISO timestamp strings.
- `activeTier`, `activePeriod`, `activeTab` — drive which render function is called by `renderAll()`.

Snapshot pruning strategy: only the most recent snapshot per calendar day is kept; the oldest entries are dropped once the count exceeds `MAX_SNAPSHOTS = 400`.

**Breakout detection algorithm** (`renderBreakouts`):
For each coin currently ranked ≤ tier, walk all historical snapshots in chronological order tracking `outsideSeen` and `entryKey`. A breakout is recorded when a coin transitions from outside → inside the tier. This means re-entries (a coin dips out then comes back) are also captured with the most recent entry date.

**Biggest movers algorithm** (`renderMovers`):
`findClosestSnapshot(targetMs)` searches all snapshot timestamps for the one nearest to `now - N*days`. `delta = historicalRank - currentRank` (positive = moved up in rank). Coins absent from the historical snapshot are silently skipped.

## Key constants to know

| Location | Constant | Default | Effect |
|---|---|---|---|
| `app.py` | `CACHE_TTL` | 300 s | How long before a new CoinGecko fetch is triggered |
| `app.py` | `time.sleep(1.5)` | 1.5 s | Inter-page pause; increase if hitting 429s |
| `index.html` | `MAX_SNAPSHOTS` | 400 | Max daily snapshots retained in localStorage |
| `index.html` | `LS_KEY` | `crt_snapshots_v2` | localStorage namespace — bump version to reset all users' history |
| `index.html` | `PAGE` (browser tab) | 100 | Max rows shown without search filter |
