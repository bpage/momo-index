"""
momo_api.py — MOMO Index backend
Uses X session cookies to call X's internal search API directly.
Requires env vars: X_AUTH_TOKEN, X_CT0
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
    'tank','tanking','loss','losses','lower','correction','overbought'
}

# X web app's public bearer token (same for all sessions)
_X_BEARER = ('AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I45Il1xs%3D'
             'bQ0JPmjU9F9ZAdrgismI3przy')

_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def score_sentiment(text):
    words = set(re.sub(r'[^a-z\s]', '', text.lower()).split())
    bull  = len(words & BULL_WORDS)
    bear  = len(words & BEAR_WORDS)
    if bull > bear:  return 'Bullish'
    if bear > bull:  return 'Bearish'
    return 'Neutral'


def x_search(query, count=100):
    """Hit X's internal v1.1 search API using session cookies."""
    auth_token = os.environ.get('X_AUTH_TOKEN', '')
    ct0        = os.environ.get('X_CT0', '')

    if not auth_token or not ct0:
        print('X_AUTH_TOKEN / X_CT0 not set')
        return []

    res = requests.get(
        'https://api.twitter.com/1.1/search/tweets.json',
        headers={
            'Authorization':  f'Bearer {_X_BEARER}',
            'Cookie':         f'auth_token={auth_token}; ct0={ct0}',
            'X-Csrf-Token':   ct0,
            'User-Agent':     ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                               'AppleWebKit/537.36 (KHTML, like Gecko) '
                               'Chrome/122.0.0.0 Safari/537.36'),
            'Referer':        'https://x.com/',
            'X-Twitter-Active-User': 'yes',
            'X-Twitter-Auth-Type':   'OAuth2Session',
            'X-Twitter-Client-Language': 'en',
        },
        params={
            'q':            query,
            'count':        count,
            'tweet_mode':   'extended',
            'result_type':  'recent',
            'lang':         'en',
        },
        timeout=15,
    )

    if res.status_code != 200:
        print(f'X API {res.status_code}: {res.text[:300]}')
        return []

    return res.json().get('statuses', [])


def build_results(statuses):
    """Bucket tweets by ticker and compute momo scores."""
    buckets = {sym: [] for sym in UNIVERSE}
    for t in statuses:
        text_up = t.get('full_text', '').upper()
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
            text = t.get('full_text', '')
            sent = score_sentiment(text)
            if sent == 'Bullish':  bull_count += 1
            elif sent == 'Bearish': bear_count += 1
            user = t.get('user', {})
            posts.append({
                'body':      text[:140],
                'sentiment': sent,
                'user':      user.get('screen_name', 'user'),
                'followers': user.get('followers_count', 0),
                'time':      t.get('created_at', ''),
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


@momo_bp.route('/api/momo')
def momo_index():
    with _lock:
        if _cache['data'] and time.time() - _cache['ts'] < CACHE_TTL:
            return jsonify(_cache['data'])

    cashtags = ' OR '.join(f'${s}' for s in UNIVERSE)
    query    = f'({cashtags}) -filter:retweets'

    try:
        statuses = x_search(query, count=100)
    except Exception as e:
        print(f'x_search error: {e}')
        return jsonify({'error': 'X fetch failed'}), 503

    if not statuses:
        return jsonify({'error': 'No tweets returned — check X_AUTH_TOKEN / X_CT0'}), 503

    results = build_results(statuses)
    if not results:
        return jsonify({'error': 'No ticker matches found'}), 503

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

    try:
        statuses = x_search(f'${sym} -filter:retweets', count=30)
    except Exception as e:
        return jsonify({'error': str(e)}), 503

    if not statuses:
        return jsonify({'error': 'No tweets found'}), 503

    bull = bear = 0
    posts = []
    for t in statuses:
        text = t.get('full_text', '')
        sent = score_sentiment(text)
        if sent == 'Bullish':  bull += 1
        elif sent == 'Bearish': bear += 1
        user = t.get('user', {})
        posts.append({
            'body':      text[:140],
            'sentiment': sent,
            'user':      user.get('screen_name', 'user'),
            'followers': user.get('followers_count', 0),
            'time':      t.get('created_at', ''),
        })

    total     = bull + bear or 1
    bull_pct  = round(bull / total * 100)
    momo_score = min(round(min(len(statuses)/30*40, 40) + bull_pct * 0.6), 100)

    return jsonify({
        'sym':       sym,
        'name':      NAMES.get(sym, sym),
        'bullCount': bull,
        'bearCount': bear,
        'bullPct':   bull_pct,
        'bearPct':   100 - bull_pct,
        'total':     len(statuses),
        'momoScore': momo_score,
        'posts':     sorted(posts, key=lambda p: p['followers'], reverse=True)[:3],
    })
