"""
momo_api.py — MOMO Index backend
Reddit sentiment from WSB + r/stocks + r/options.
Background thread refreshes all tickers sequentially every 5 min.
No API key required.
"""

from flask import Blueprint, jsonify
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

MULTI_SUB = 'wallstreetbets+stocks+options+investing+stockmarket'
HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/122.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}

_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()
CACHE_TTL = 300  # 5 minutes


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
    """One request covering all subreddits for a single ticker."""
    try:
        url = (f'https://www.reddit.com/r/{MULTI_SUB}/search.json'
               f'?q=%24{sym}&restrict_sr=on&sort=new&t=day&limit=25')
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            print(f'Reddit {res.status_code} for {sym}')
            return []
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
        return posts
    except Exception as e:
        print(f'Reddit error {sym}: {e}')
        return []


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


def _refresh_cache():
    """Fetch all tickers one at a time — avoids Reddit rate limits."""
    results = []
    for sym in UNIVERSE:
        posts = fetch_reddit(sym)
        data  = calc_ticker(sym, posts)
        if data:
            results.append(data)
        time.sleep(1)  # 1s between requests — polite and avoids rate limits
    if not results:
        print('Cache refresh: no results')
        return
    payload = {
        'stocks':    results,
        'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    with _lock:
        _cache['data'] = payload
        _cache['ts']   = time.time()
    print(f'Cache refreshed: {len(results)} tickers')


def _schedule_refresh():
    while True:
        try:
            _refresh_cache()
        except Exception as e:
            print(f'Refresh error: {e}')
        time.sleep(CACHE_TTL)


# Pre-warm cache on startup
threading.Thread(target=_schedule_refresh, daemon=True).start()


@momo_bp.route('/api/momo')
def momo_index():
    with _lock:
        data = _cache['data']
    if not data:
        return jsonify({'error': 'Warming up — check back in 60 seconds'}), 503
    return jsonify(data)


@momo_bp.route('/api/momo/ticker/<sym>')
def momo_ticker(sym):
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404
    posts = fetch_reddit(sym)
    data  = calc_ticker(sym, posts)
    if not data:
        return jsonify({'error': 'No Reddit posts found'}), 503
    return jsonify(data)
