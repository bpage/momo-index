"""
momo_api.py — MOMO Index backend
Uses twscrape to pull real X cashtag data via your X account login.
Requires env vars: X_USERNAME, X_PASSWORD, X_EMAIL
"""

from flask import Blueprint, jsonify
import asyncio
import threading
import time
import re
import os

from twscrape import API as TwAPI

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
    'tank','tanking','loss','losses','lower','correction','overbought'
}

_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def run_async(coro):
    """Run an async coroutine from sync Flask code."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def score_sentiment(text):
    words = set(re.sub(r'[^a-z\s]', '', text.lower()).split())
    bull  = len(words & BULL_WORDS)
    bear  = len(words & BEAR_WORDS)
    if bull > bear:  return 'Bullish'
    if bear > bull:  return 'Bearish'
    return 'Neutral'


async def _fetch_tweets():
    """Authenticate with X via twscrape and run a batched cashtag search."""
    username = os.environ.get('X_USERNAME', '')
    password = os.environ.get('X_PASSWORD', '')
    email    = os.environ.get('X_EMAIL', '')

    if not all([username, password, email]):
        print('X credentials not set')
        return []

    api = TwAPI()  # loads any saved session from accounts.db

    # Add account if not already saved; no-op if already present
    await api.pool.add_account(username, password, email, password)
    await api.pool.login_all()

    cashtags = ' OR '.join(f'${s}' for s in UNIVERSE)
    query    = f'({cashtags}) -filter:retweets lang:en'

    tweets = []
    async for tweet in api.search(query, limit=100):
        tweets.append(tweet)
    return tweets


def build_results(raw_tweets):
    """Bucket tweets by ticker and compute per-ticker momo scores."""
    buckets = {sym: [] for sym in UNIVERSE}
    for tweet in raw_tweets:
        text_up = tweet.rawContent.upper()
        for sym in UNIVERSE:
            if f'${sym}' in text_up:
                buckets[sym].append(tweet)

    results = []
    for sym in UNIVERSE:
        tw = buckets[sym]
        if not tw:
            continue

        bull_count = bear_count = 0
        posts = []
        for t in tw:
            sent = score_sentiment(t.rawContent)
            if sent == 'Bullish':  bull_count += 1
            elif sent == 'Bearish': bear_count += 1
            posts.append({
                'body':      t.rawContent[:140],
                'sentiment': sent,
                'user':      t.user.username,
                'followers': t.user.followersCount,
                'time':      t.date.strftime('%Y-%m-%dT%H:%M:%SZ'),
            })

        total     = bull_count + bear_count or 1
        bull_pct  = round(bull_count / total * 100)
        bear_pct  = 100 - bull_pct

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
            'posts':     sorted(posts, key=lambda p: p['followers'], reverse=True)[:3],
        })

    return results


@momo_bp.route('/api/momo/debug')
def momo_debug():
    """GET /api/momo/debug — check login status and account pool."""
    async def _check():
        username = os.environ.get('X_USERNAME', '')
        password = os.environ.get('X_PASSWORD', '')
        email    = os.environ.get('X_EMAIL', '')
        if not all([username, password, email]):
            return {'error': 'credentials not set'}
        api = TwAPI()
        await api.pool.add_account(username, password, email, password)
        await api.pool.login_all()
        accounts = await api.pool.get_all()
        return [{
            'username': a.username,
            'active':   a.active,
            'locks':    a.locks,
            'error':    a.error,
        } for a in accounts]
    try:
        result = run_async(_check())
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 503


@momo_bp.route('/api/momo')
def momo_index():
    with _lock:
        if _cache['data'] and time.time() - _cache['ts'] < CACHE_TTL:
            return jsonify(_cache['data'])

    try:
        raw = run_async(_fetch_tweets())
    except Exception as e:
        print(f'twscrape error: {e}')
        return jsonify({'error': 'X fetch failed'}), 503

    if not raw:
        return jsonify({'error': 'No tweets returned — check credentials'}), 503

    results = build_results(raw)
    if not results:
        return jsonify({'error': 'No ticker matches in tweets'}), 503

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
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404

    async def _single():
        username = os.environ.get('X_USERNAME', '')
        password = os.environ.get('X_PASSWORD', '')
        email    = os.environ.get('X_EMAIL', '')
        api = TwAPI()
        await api.pool.add_account(username, password, email, password)
        await api.pool.login_all()
        tweets = []
        async for t in api.search(f'${sym} -filter:retweets lang:en', limit=30):
            tweets.append(t)
        return tweets

    try:
        raw = run_async(_single())
    except Exception as e:
        return jsonify({'error': str(e)}), 503

    bull = bear = 0
    posts = []
    for t in raw:
        sent = score_sentiment(t.rawContent)
        if sent == 'Bullish':  bull += 1
        elif sent == 'Bearish': bear += 1
        posts.append({
            'body':      t.rawContent[:140],
            'sentiment': sent,
            'user':      t.user.username,
            'followers': t.user.followersCount,
            'time':      t.date.strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

    total     = bull + bear or 1
    bull_pct  = round(bull / total * 100)
    momo_score = min(round(min(len(raw)/30*40, 40) + bull_pct * 0.6), 100)

    return jsonify({
        'sym':       sym,
        'name':      NAMES.get(sym, sym),
        'bullCount': bull,
        'bearCount': bear,
        'bullPct':   bull_pct,
        'bearPct':   100 - bull_pct,
        'total':     len(raw),
        'momoScore': momo_score,
        'posts':     sorted(posts, key=lambda p: p['followers'], reverse=True)[:3],
    })
