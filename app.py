"""
Crypto Rank Tracker — Flask Backend
"""

import os
import time
import threading
import datetime
import functools
import requests
import psycopg2
from psycopg2.extras import Json
from flask import Flask, jsonify, render_template, session, request
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pyotp
import pytz

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

# ---------------------------------------------------------------------------
# In-memory cache (CoinGecko responses)
# ---------------------------------------------------------------------------
_cache = {"data": None, "ts": 0}
_cache_lock = threading.Lock()
CACHE_TTL = 300

_mem_snapshots = {}
_mem_lock = threading.Lock()

# In-memory user store (fallback when no DATABASE_URL)
_mem_users = {}       # email -> user dict
_mem_users_id = {}    # id -> user dict
_mem_users_lock = threading.Lock()
_mem_user_seq = [1]

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {"Accept": "application/json", "User-Agent": "CryptoRankTracker/1.0"}


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
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id            SERIAL PRIMARY KEY,
                        email         TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        totp_secret   TEXT,
                        created_at    TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------
def _get_user_by_email(email):
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, email, password_hash, totp_secret FROM users WHERE email = %s",
                    (email,)
                )
                row = cur.fetchone()
                if row:
                    return {'id': row[0], 'email': row[1], 'password_hash': row[2], 'totp_secret': row[3]}
        finally:
            conn.close()
        return None
    with _mem_users_lock:
        return _mem_users.get(email)


def _get_user_by_id(user_id):
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, email, password_hash, totp_secret FROM users WHERE id = %s",
                    (user_id,)
                )
                row = cur.fetchone()
                if row:
                    return {'id': row[0], 'email': row[1], 'password_hash': row[2], 'totp_secret': row[3]}
        finally:
            conn.close()
        return None
    with _mem_users_lock:
        return _mem_users_id.get(user_id)


def _create_user(email, password_hash):
    conn = _get_conn()
    if conn:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                        (email, password_hash)
                    )
                    return cur.fetchone()[0]
        finally:
            conn.close()
        return None
    with _mem_users_lock:
        uid = _mem_user_seq[0]
        _mem_user_seq[0] += 1
        user = {'id': uid, 'email': email, 'password_hash': password_hash, 'totp_secret': None}
        _mem_users[email] = user
        _mem_users_id[uid] = user
        return uid


def _update_user_totp(user_id, totp_secret):
    conn = _get_conn()
    if conn:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET totp_secret = %s WHERE id = %s",
                        (totp_secret, user_id)
                    )
        finally:
            conn.close()
        return
    with _mem_users_lock:
        user = _mem_users_id.get(user_id)
        if user:
            user['totp_secret'] = totp_secret
            _mem_users[user['email']]['totp_secret'] = totp_secret


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return jsonify({'error': 'Unauthorized', 'code': 'auth_required'}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------
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
    return [
        {"r": c["rank"], "i": c["id"], "n": c["name"], "s": c["symbol"],
         "cg": c["cgUrl"], "cmc": c["cmcUrl"], "img": c.get("image", ""),
         "p": c.get("price"), "ch": c.get("change24h"), "mc": c.get("marketCap")}
        for c in coins
    ]


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route('/auth/me')
def auth_me():
    uid = session.get('user_id')
    if not uid:
        return jsonify({'loggedIn': False})
    user = _get_user_by_id(uid)
    if not user:
        session.clear()
        return jsonify({'loggedIn': False})
    return jsonify({'loggedIn': True, 'email': user['email'], 'has2fa': bool(user['totp_secret'])})


@app.route('/auth/signup', methods=['POST'])
def auth_signup():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    if _get_user_by_email(email):
        return jsonify({'error': 'Email already registered'}), 409
    pw_hash = generate_password_hash(password)
    uid = _create_user(email, pw_hash)
    if not uid:
        return jsonify({'error': 'Failed to create account'}), 500
    session['user_id'] = uid
    session['email'] = email
    return jsonify({'ok': True, 'email': email, 'has2fa': False})


@app.route('/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    user = _get_user_by_email(email)
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid email or password'}), 401
    if user['totp_secret']:
        session['pending_2fa_uid'] = user['id']
        session['pending_2fa_email'] = email
        return jsonify({'ok': True, 'requires2fa': True})
    session['user_id'] = user['id']
    session['email'] = email
    return jsonify({'ok': True, 'requires2fa': False, 'email': email})


@app.route('/auth/verify-2fa-login', methods=['POST'])
def auth_verify_2fa_login():
    data = request.get_json() or {}
    code = (data.get('code') or '').strip()
    uid = session.get('pending_2fa_uid')
    email = session.get('pending_2fa_email')
    if not uid:
        return jsonify({'error': 'No pending 2FA session'}), 400
    user = _get_user_by_id(uid)
    if not user or not user['totp_secret']:
        return jsonify({'error': 'User not found'}), 404
    if not pyotp.TOTP(user['totp_secret']).verify(code):
        return jsonify({'error': 'Invalid code'}), 401
    session.pop('pending_2fa_uid', None)
    session.pop('pending_2fa_email', None)
    session['user_id'] = uid
    session['email'] = email
    return jsonify({'ok': True, 'email': email})


@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/auth/setup-2fa', methods=['POST'])
@login_required
def auth_setup_2fa():
    email = session.get('email')
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name='CryptoRankTracker')
    session['pending_totp_secret'] = secret
    return jsonify({'secret': secret, 'uri': uri})


@app.route('/auth/enable-2fa', methods=['POST'])
@login_required
def auth_enable_2fa():
    data = request.get_json() or {}
    code = (data.get('code') or '').strip()
    secret = session.get('pending_totp_secret')
    if not secret:
        return jsonify({'error': 'No pending 2FA setup'}), 400
    if not pyotp.TOTP(secret).verify(code):
        return jsonify({'error': 'Invalid code — please try again'}), 401
    _update_user_totp(session['user_id'], secret)
    session.pop('pending_totp_secret', None)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# App routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/top1000")
@login_required
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
@login_required
def api_snapshots():
    if _db_url():
        snapshots = _load_snapshots_db() or {}
    else:
        with _mem_lock:
            snapshots = dict(_mem_snapshots)
    return jsonify({"snapshots": snapshots, "count": len(snapshots)})


@app.route("/api/snapshots/clear", methods=["POST"])
@login_required
def api_snapshots_clear():
    if _db_url():
        _clear_snapshots_db()
    else:
        with _mem_lock:
            _mem_snapshots.clear()
    return jsonify({"ok": True})


@app.route("/api/cache/clear", methods=["POST"])
@login_required
def clear_cache():
    with _cache_lock:
        _cache["data"] = None
        _cache["ts"] = 0
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Scheduled daily snapshot
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
