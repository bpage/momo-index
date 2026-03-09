"""
momo_api.py — MOMO Index backend
Pulls real-time Reddit sentiment from WSB + r/stocks + r/options.
Uses multi-subreddit batching + parallel fetches to stay fast.
No API key required.
"""

from flask import Blueprint, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import threading
import time
import re

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
    'surge','rocket','gains','higher','bounce','oversold','accumulate',
    'yolo','tendies','printing','mooning','send','run','explode'
}
BEAR_WORDS = {
    'bear','bearish','short','sell','puts','dump','crash','drop',
    'breakdown','downside','resistance','weak','red','falling','collapse',
    'tank','tanking','loss','losses','lower','correction','overbought',
    'bubble','overvalued','topped','bagholder','drilling','rekt'
}

# One request covers all 5 subreddits at once
MULTI_SUB = 'wallstreetbets+stocks+options+investing+stockmarket'

_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()
CACHE_TTL = 300  # 5 minutes

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/122.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def score_sentiment(text, upvote_ratio=0.5):
    words = set(re.sub(r'[^a-z\s]', '', text.lower()).split())
    bull  = len(words & BULL_WORDS)
    bear  = len(words & BEAR_WORDS)
    if upvote_ratio > 0.75: bull += 1
    elif upvote_ratio < 0.40: bear += 1
    if bull > bear:  return 'Bullish'
    if bear > bull:  return 'Bearish'
    return 'Neutral'


def fetch_reddit(sym):
    """Single request across all subreddits for one ticker."""
    try:
        url = (f'https://www.reddit.com/r/{MULTI_SUB}/search.json'
               f'?q=%24{sym}&restrict_sr=on&sort=new&t=day&limit=25')
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            print(f'Reddit {res.status_code} for {sym}')
            return sym, []
        posts = []
        for c in res.json().get('data', {}).get('children', []):
            d = c.get('data', {})
            posts.append({
                'title':        d.get('title', ''),
                'score':        d.get('score', 0),
                'upvote_ratio': d.get('upvote_ratio', 0.5),
                'author':       d.get('author', 'anon'),
                'created_utc':  d.get('created_utc', 0),
                'subreddit':    d.get('subreddit', ''),
            })
        return sym, posts
    except Exception as e:
        print(f'Reddit error {sym}: {e}')
        return sym, []


def calc_ticker(sym, posts):
    if not posts:
        return None
    bull_count = bear_count = 0
    feed_posts = []
    for p in posts:
        sent = score_sentiment(p['title'], p['upvote_ratio'])
        if sent == 'Bullish':  bull_count += 1
        elif sent == 'Bearish': bear_count += 1
        feed_posts.append({
            'body':      f"[r/{p['subreddit']}] {p['title'][:120]}",
            'sentiment': sent,
            'user':      p['author'],
            'followers': p['score'],
            'time':      time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                       time.gmtime(p['created_utc'])),
        })
    total    = bull_count + bear_count or 1
    bull_pct = round(bull_count / total * 100)
    vol_score  = min(len(posts) / 20 * 40, 40)
    momo_score = min(round(vol_score + bull_pct * 0.6), 100)
    return {
        'sym':       sym,
        'name':      NAMES.get(sym, sym),
        'bullCount': bull_count,
        'bearCount': bear_count,
        'bullPct':   bull_pct,
        'bearPct':   100 - bull_pct,
        'total':     len(posts),
        'momoScore': momo_score,
        'posts':     sorted(feed_posts, key=lambda p: p['followers'], reverse=True)[:3],
    }


@momo_bp.route('/api/momo')
def momo_index():
    with _lock:
        if _cache['data'] and time.time() - _cache['ts'] < CACHE_TTL:
            return jsonify(_cache['data'])

    results = []
    # Fetch all 20 tickers in parallel (3 workers to avoid Reddit rate limits)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_reddit, sym): sym for sym in UNIVERSE}
        for future in as_completed(futures):
            sym, posts = future.result()
            data = calc_ticker(sym, posts)
            if data:
                results.append(data)

    if not results:
        return jsonify({'error': 'Reddit unavailable'}), 503

    # Preserve UNIVERSE order
    order = {s: i for i, s in enumerate(UNIVERSE)}
    results.sort(key=lambda r: order.get(r['sym'], 99))

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
    _, posts = fetch_reddit(sym)
    data = calc_ticker(sym, posts)
    if not data:
        return jsonify({'error': 'No Reddit posts found'}), 503
    return jsonify(data)
