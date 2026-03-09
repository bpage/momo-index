"""
momo_api.py — MOMO Index backend
Fetches real-time X (Twitter) cashtag data via the X API v2.
Requires env var: X_BEARER_TOKEN
"""

from flask import Blueprint, jsonify
import requests
import threading
import time
import re
import os

momo_bp = Blueprint('momo', __name__)

UNIVERSE = [
    'NVDA','TSLA','AAPL','META','AMZN','MSFT','GOOGL','AMD',
    'COIN','MSTR','HOOD','PLTR','SOFI','SMCI','RIVN',
    'SPOT','CRWD','MELI','SHOP','SPY'
]

NAMES = {
    'NVDA':'Nvidia','TSLA':'Tesla','AAPL':'Apple','META':'Meta',
    'AMZN':'Amazon','MSFT':'Microsoft','GOOGL':'Alphabet','AMD':'AMD',
    'COIN':'Coinbase','MSTR':'MicroStrategy','HOOD':'Robinhood',
    'PLTR':'Palantir','SOFI':'SoFi','SMCI':'Supermicro','RIVN':'Rivian',
    'SPOT':'Spotify','CRWD':'CrowdStrike','MELI':'MercadoLibre',
    'SHOP':'Shopify','SPY':'S&P 500 ETF'
}

BULL_WORDS = {
    'bull','bullish','long','buy','calls','moon','pump','rip','breakout',
    'upside','support','hold','strong','ath','green','squeeze','rally',
    'surge','rocket','gains','higher','bounce','oversold','accumulate'
}
BEAR_WORDS = {
    'bear','bearish','short','sell','puts','dump','crash','drop',
    'breakdown','downside','resistance','weak','red','falling','collapse',
    'tank','tanking','loss','losses','lower','correction','overbought','distribution'
}

# X free tier: 1 search req / 15 min — cache aggressively
_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()
CACHE_TTL = 900  # 15 minutes


def score_sentiment(text):
    words = set(re.sub(r'[^a-z\s]', '', text.lower()).split())
    bull = len(words & BULL_WORDS)
    bear = len(words & BEAR_WORDS)
    if bull > bear:  return 'Bullish'
    if bear > bull:  return 'Bearish'
    return 'Neutral'


def fetch_x():
    """Single batched X API query for all 20 tickers at once."""
    token = os.environ.get('X_BEARER_TOKEN', '')
    if not token:
        print('X_BEARER_TOKEN not set')
        return None

    cashtags = ' OR '.join(f'${s}' for s in UNIVERSE)
    query    = f'({cashtags}) -is:retweet lang:en'

    res = requests.get(
        'https://api.twitter.com/2/tweets/search/recent',
        headers={'Authorization': f'Bearer {token}'},
        params={
            'query':        query,
            'max_results':  100,
            'tweet.fields': 'created_at,text',
            'expansions':   'author_id',
            'user.fields':  'username,public_metrics',
        },
        timeout=15,
    )

    if res.status_code != 200:
        print(f'X API {res.status_code}: {res.text[:200]}')
        return None
    return res.json()


def build_results(raw):
    """Parse raw X API response into per-ticker momo scores."""
    tweets = raw.get('data', [])
    users  = {u['id']: u for u in raw.get('includes', {}).get('users', [])}

    # Bucket each tweet into whichever tickers it mentions
    buckets = {sym: [] for sym in UNIVERSE}
    for t in tweets:
        text_up = t.get('text', '').upper()
        for sym in UNIVERSE:
            if f'${sym}' in text_up:
                buckets[sym].append(t)

    results = []
    for sym in UNIVERSE:
        tw = buckets[sym]
        if not tw:
            continue

        bull_count = bear_count = 0
        posts = []
        for t in tw:
            sent = score_sentiment(t.get('text', ''))
            if sent == 'Bullish':  bull_count += 1
            elif sent == 'Bearish': bear_count += 1
            author = users.get(t.get('author_id', ''), {})
            posts.append({
                'body':      t.get('text', '')[:140],
                'sentiment': sent,
                'user':      author.get('username', 'user'),
                'followers': author.get('public_metrics', {}).get('followers_count', 0),
                'time':      t.get('created_at', ''),
            })

        total     = bull_count + bear_count or 1
        bull_pct  = round(bull_count / total * 100)
        bear_pct  = 100 - bull_pct

        # Momo = tweet volume surge (0-40) + sentiment strength (0-60)
        vol_score  = min(len(tw) / 30 * 40, 40)
        momo_score = min(round(vol_score + bull_pct * 0.6), 100)

        results.append({
            'sym':       sym,
            'name':      NAMES.get(sym, sym),
            'bullCount': bull_count,
            'bearCount': bear_count,
            'bullPct':   bull_pct,
            'bearPct':   bear_pct,
            'total':     len(tw),
            'momoScore': momo_score,
            # Top 3 posts sorted by follower count
            'posts': sorted(posts, key=lambda p: p['followers'], reverse=True)[:3],
        })

    return results


@momo_bp.route('/api/momo')
def momo_index():
    """GET /api/momo — all tickers, cached up to 15 min (X rate limit)."""
    with _lock:
        if _cache['data'] and time.time() - _cache['ts'] < CACHE_TTL:
            return jsonify(_cache['data'])

    try:
        raw = fetch_x()
    except Exception as e:
        print(f'fetch_x error: {e}')
        return jsonify({'error': 'X API unavailable'}), 503

    if not raw:
        return jsonify({'error': 'X API unavailable'}), 503

    results = build_results(raw)
    if not results:
        return jsonify({'error': 'No ticker data in response'}), 503

    payload = {
        'stocks':    results,
        'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    with _lock:
        _cache['data'] = payload
        _cache['ts']   = time.time()

    return jsonify(payload)


@momo_bp.route('/api/momo/ticker/<sym>')
def momo_ticker(sym):
    """GET /api/momo/ticker/NVDA — single ticker, live (no cache)."""
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404

    token = os.environ.get('X_BEARER_TOKEN', '')
    if not token:
        return jsonify({'error': 'X_BEARER_TOKEN not set'}), 503

    try:
        res = requests.get(
            'https://api.twitter.com/2/tweets/search/recent',
            headers={'Authorization': f'Bearer {token}'},
            params={
                'query':        f'${sym} -is:retweet lang:en',
                'max_results':  30,
                'tweet.fields': 'created_at,text',
                'expansions':   'author_id',
                'user.fields':  'username,public_metrics',
            },
            timeout=15,
        )
        if res.status_code != 200:
            return jsonify({'error': f'X API {res.status_code}'}), 503

        raw    = res.json()
        tweets = raw.get('data', [])
        users  = {u['id']: u for u in raw.get('includes', {}).get('users', [])}

        bull = bear = 0
        posts = []
        for t in tweets:
            sent = score_sentiment(t.get('text', ''))
            if sent == 'Bullish':  bull += 1
            elif sent == 'Bearish': bear += 1
            author = users.get(t.get('author_id', ''), {})
            posts.append({
                'body':      t.get('text', '')[:140],
                'sentiment': sent,
                'user':      author.get('username', 'user'),
                'followers': author.get('public_metrics', {}).get('followers_count', 0),
                'time':      t.get('created_at', ''),
            })

        total     = bull + bear or 1
        bull_pct  = round(bull / total * 100)
        momo_score = min(round(min(len(tweets)/30*40, 40) + bull_pct * 0.6), 100)

        return jsonify({
            'sym':       sym,
            'name':      NAMES.get(sym, sym),
            'bullCount': bull,
            'bearCount': bear,
            'bullPct':   bull_pct,
            'bearPct':   100 - bull_pct,
            'total':     len(tweets),
            'momoScore': momo_score,
            'posts':     sorted(posts, key=lambda p: p['followers'], reverse=True)[:3],
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 503
