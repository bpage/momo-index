"""
momo_api.py — MOMO Index backend
Fetches StockTwits, Reddit, and X (Twitter) data server-side.
Blends three social signals into a unified MOMO score.
Background APScheduler job refreshes social data every 20 minutes.

Score weights:
  StockTwits : 60%  (volume + sentiment)
  Reddit     : 25%  (engagement on r/wsb, r/stocks, r/investing, r/options)
  X          : 15%  (cashtag engagement — requires X_BEARER_TOKEN env var)
"""

import os
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, jsonify
import requests

# Social scanners
try:
    from reddit_scanner import get_reddit_signal
except ImportError:
    def get_reddit_signal(universe): return {}

try:
    from x_scanner import get_x_signal
except ImportError:
    def get_x_signal(universe): return {}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger(__name__)

momo_bp = Blueprint('momo', __name__)

UNIVERSE = [
    'NVDA', 'TSLA', 'AAPL', 'META', 'AMZN', 'MSFT', 'GOOGL', 'AMD',
    'COIN', 'MSTR', 'HOOD', 'PLTR', 'SOFI', 'APP', 'IBIT',
    'SPOT', 'CRWD', 'RKLB', 'HIMS', 'CEG',
]

NAMES = {
    'NVDA': 'Nvidia', 'TSLA': 'Tesla', 'AAPL': 'Apple', 'META': 'Meta',
    'AMZN': 'Amazon', 'MSFT': 'Microsoft', 'GOOGL': 'Alphabet', 'AMD': 'AMD',
    'COIN': 'Coinbase', 'MSTR': 'MicroStrategy', 'HOOD': 'Robinhood',
    'PLTR': 'Palantir', 'SOFI': 'SoFi', 'APP': 'AppLovin', 'IBIT': 'iShares Bitcoin ETF',
    'SPOT': 'Spotify', 'CRWD': 'CrowdStrike', 'RKLB': 'Rocket Lab', 'HIMS': 'Hims & Hers',
    'CEG': 'Constellation Energy',
}

# ─── Score weights ──────────────────────────────────────────────────────────
WEIGHT_STOCKTWITS = 0.60
WEIGHT_REDDIT     = 0.25
WEIGHT_X          = 0.15

SCAN_INTERVAL_MINUTES = 20

# ─── In-memory social cache ──────────────────────────────────────────────────
_social_lock = threading.Lock()
_social_cache = {
    'reddit': {},       # {sym: score_0_100}
    'x': {},            # {sym: score_0_100}
    'last_scan_at': None,
    'scan_count': 0,
}


def _run_social_scan():
    """Fetch Reddit + X signals and update in-memory cache."""
    log.info("[momo] Starting social scan...")
    scan_start = time.time()

    reddit_scores = get_reddit_signal(UNIVERSE)
    x_scores      = get_x_signal(UNIVERSE)

    elapsed = round(time.time() - scan_start, 1)
    log.info(
        f"[momo] Social scan complete in {elapsed}s — "
        f"Reddit: {len(reddit_scores)} tickers, X: {len(x_scores)} tickers"
    )

    with _social_lock:
        _social_cache['reddit'] = reddit_scores
        _social_cache['x']      = x_scores
        _social_cache['last_scan_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        _social_cache['scan_count']  += 1


def _start_scheduler():
    """Start the background social scan scheduler using a daemon thread loop."""
    def _loop():
        # Initial scan on startup (delay 10s to let Flask finish booting)
        time.sleep(10)
        while True:
            try:
                _run_social_scan()
            except Exception as e:
                log.error(f"[momo] Social scan error: {e}")
            time.sleep(SCAN_INTERVAL_MINUTES * 60)

    t = threading.Thread(target=_loop, name='social-scanner', daemon=True)
    t.start()
    log.info(f"[momo] Social scanner started — refreshing every {SCAN_INTERVAL_MINUTES}m")


# ─── Keep-alive pinger ───────────────────────────────────────────────────────

def _start_keepalive():
    """
    Ping our own /api/momo/social-status every 10 minutes via the external URL
    so Render's free-tier spin-down timer is reset by real inbound traffic.
    Uses RENDER_EXTERNAL_URL if available, falls back to the production hostname.
    """
    external_url = os.environ.get(
        'RENDER_EXTERNAL_URL', 'https://momo-index.onrender.com'
    ).rstrip('/')
    ping_url = f"{external_url}/api/momo/social-status"

    def _loop():
        time.sleep(60)  # Let Flask finish booting before first ping
        while True:
            try:
                r = requests.get(ping_url, timeout=15)
                log.info(f"[keepalive] Pinged {ping_url} -> {r.status_code}")
            except Exception as e:
                log.warning(f"[keepalive] Ping failed: {e}")
            time.sleep(10 * 60)  # 10 minutes

    t = threading.Thread(target=_loop, name='keepalive', daemon=True)
    t.start()
    log.info(f"[keepalive] Started — pinging {ping_url} every 10min")


# Start scheduler when blueprint is imported
_start_scheduler()
_start_keepalive()


# ─── StockTwits fetcher ──────────────────────────────────────────────────────

def fetch_stocktwits(sym):
    """Fetch sentiment data for one ticker from StockTwits."""
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json?limit=30"
    try:
        res = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        if res.status_code != 200:
            return None
        data = res.json()
        messages = data.get('messages', [])

        bull_count = 0
        bear_count = 0
        posts = []

        for m in messages:
            sent = (m.get('entities') or {}).get('sentiment', {})
            sentiment = sent.get('basic') if sent else None
            if sentiment == 'Bullish':
                bull_count += 1
            elif sentiment == 'Bearish':
                bear_count += 1

            posts.append({
                'body': m.get('body', '')[:140],
                'sentiment': sentiment or 'Neutral',
                'user': m.get('user', {}).get('username', 'trader'),
                'followers': m.get('user', {}).get('followers', 0),
                'time': m.get('created_at', ''),
            })

        total = len(messages) or 1
        bull_pct = round(bull_count / total * 100)
        bear_pct = round(bear_count / total * 100)

        # StockTwits momo component: volume surge + sentiment strength
        vol_score  = min(len(messages) / 30 * 40, 40)
        sent_score = bull_pct * 0.6
        st_score   = round(vol_score + sent_score)  # 0–100

        return {
            'sym': sym,
            'name': NAMES.get(sym, sym),
            'bullCount': bull_count,
            'bearCount': bear_count,
            'bullPct': bull_pct,
            'bearPct': bear_pct,
            'total': len(messages),
            'stScore': st_score,
            'posts': posts[:3],
        }
    except Exception as e:
        log.warning(f"[stocktwits] Error fetching {sym}: {e}")
        return None


def _blend_score(sym: str, st_score: float) -> dict:
    """
    Blend StockTwits, Reddit, and X scores into a single MOMO score.

    Returns a dict with momoScore and per-source breakdowns.
    """
    with _social_lock:
        reddit_score = _social_cache['reddit'].get(sym, 0.0)
        x_score      = _social_cache['x'].get(sym, 0.0)
        has_x        = bool(_social_cache['x'])

    # If X is available, redistribute weights; otherwise split between ST and Reddit
    if has_x:
        w_st = WEIGHT_STOCKTWITS
        w_rd = WEIGHT_REDDIT
        w_x  = WEIGHT_X
    else:
        # Normalize ST and Reddit to fill 100%
        w_st = WEIGHT_STOCKTWITS / (WEIGHT_STOCKTWITS + WEIGHT_REDDIT)
        w_rd = WEIGHT_REDDIT     / (WEIGHT_STOCKTWITS + WEIGHT_REDDIT)
        w_x  = 0.0

    momo_score = round(
        st_score     * w_st +
        reddit_score * w_rd +
        x_score      * w_x
    )

    return {
        'momoScore':    momo_score,
        'stScore':      round(st_score, 1),
        'redditScore':  round(reddit_score, 1),
        'xScore':       round(x_score, 1) if has_x else None,
    }


# ─── Routes ──────────────────────────────────────────────────────────────────

@momo_bp.route('/api/momo')
def momo_index():
    """
    GET /api/momo
    Returns blended social sentiment + momo scores for all tickers.
    Called by the dashboard every 90 seconds.
    """
    with _social_lock:
        last_scan_at = _social_cache['last_scan_at']
        scan_count   = _social_cache['scan_count']

    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_stocktwits, sym): sym for sym in UNIVERSE}
        for fut in as_completed(futures):
            st_data = fut.result()
            if not st_data:
                continue
            blend = _blend_score(st_data['sym'], st_data['stScore'])
            results.append({**st_data, **blend})

    if not results:
        return jsonify({'error': 'StockTwits unavailable'}), 503

    log.info(f"[momo] /api/momo served {len(results)} tickers (scan #{scan_count})")

    return jsonify({
        'stocks':      results,
        'fetchedAt':   time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'lastSocialScanAt': last_scan_at,
        'socialScanCount':  scan_count,
        'sources': {
            'stocktwits': True,
            'reddit':     bool(_social_cache['reddit']),
            'x':          bool(_social_cache['x']),
        },
    })


@momo_bp.route('/api/momo/ticker/<sym>')
def momo_ticker(sym):
    """
    GET /api/momo/ticker/NVDA
    Single ticker lookup — useful for debugging.
    """
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404

    st_data = fetch_stocktwits(sym)
    if not st_data:
        return jsonify({'error': 'Failed to fetch StockTwits data'}), 503

    blend = _blend_score(sym, st_data['stScore'])
    return jsonify({**st_data, **blend})


@momo_bp.route('/api/momo/social-status')
def social_status():
    """
    GET /api/momo/social-status
    Returns current social cache state — useful for monitoring scans.
    """
    with _social_lock:
        reddit_top5 = sorted(
            _social_cache['reddit'].items(), key=lambda x: -x[1]
        )[:5]
        x_top5 = sorted(
            _social_cache['x'].items(), key=lambda x: -x[1]
        )[:5]
        return jsonify({
            'lastScanAt':    _social_cache['last_scan_at'],
            'scanCount':     _social_cache['scan_count'],
            'scanIntervalM': SCAN_INTERVAL_MINUTES,
            'reddit': {
                'tickersFound': len(_social_cache['reddit']),
                'top5': reddit_top5,
            },
            'x': {
                'enabled':      bool(_social_cache['x']),
                'tickersFound': len(_social_cache['x']),
                'top5': x_top5,
            },
        })
