"""
momo_api.py — MOMO Index backend
X/Twitter sentiment via Apify tweet-scraper (apidojo~tweet-scraper).
Background thread refreshes every 2 hours. Reddit removed.
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
    'surge','rocket','gains','higher','bounce','oversold','accumulate',
    'yolo','tendies','printing','mooning','send','run','explode'
}
BEAR_WORDS = {
    'bear','bearish','short','sell','puts','dump','crash','drop',
    'breakdown','downside','resistance','weak','red','falling','collapse',
    'tank','tanking','loss','losses','lower','correction','overbought',
    'bubble','overvalued','topped','bagholder','drilling','rekt'
}

# ── Apify config ──────────────────────────────────────────────────────────────
APIFY_TOKEN = os.environ.get('APIFY_TOKEN', '')
APIFY_ACTOR = 'apidojo~tweet-scraper'
APIFY_SYNC  = (f'https://api.apify.com/v2/acts/{APIFY_ACTOR}'
               f'/run-sync-get-dataset-items')

REFRESH_INTERVAL = 7200   # 2 hours between Apify runs

# Pre-build Twitter search URLs for each ticker (startUrls format works; searchTerms doesn't)
_SEARCH_URLS = [
    {'url': f'https://twitter.com/search?q=%24{sym}&src=typed_query&f=live'}
    for sym in UNIVERSE
]

# ── State ─────────────────────────────────────────────────────────────────────
_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()

# ── Sentiment ─────────────────────────────────────────────────────────────────
def score_sentiment(text):
    words = set(re.sub(r'[^a-z\s]', '', text.lower()).split())
    bull  = len(words & BULL_WORDS)
    bear  = len(words & BEAR_WORDS)
    if bull > bear:  return 'Bullish'
    if bear > bull:  return 'Bearish'
    return 'Neutral'

# ── Apify fetch ───────────────────────────────────────────────────────────────
def fetch_all_tickers():
    """
    One Apify run: 20 individual search terms, 10 tweets each = ~200 tweets.
    Cost: ~$0.08 per run.
    """
    if not APIFY_TOKEN:
        print('No APIFY_TOKEN set')
        return {}

    payload = {
        'startUrls':     _SEARCH_URLS,   # real Twitter search URLs, one per ticker
        'maxItems':      200,
        'sort':          'Latest',
        'tweetLanguage': 'en',
    }
    print(f'Apify: fetching X data for {len(UNIVERSE)} tickers...')
    try:
        res = requests.post(
            APIFY_SYNC,
            params={'token': APIFY_TOKEN},
            json=payload,
            timeout=90,   # well under gunicorn --timeout 300
        )
    except Exception as e:
        print(f'Apify request error: {e}')
        return {}

    # run-sync-get-dataset-items returns 201 on success
    if res.status_code not in (200, 201):
        print(f'Apify HTTP {res.status_code}: {res.text[:200]}')
        return {}

    try:
        items = res.json()
    except Exception:
        print('Apify: invalid JSON response')
        return {}

    # Filter out noResults placeholders
    tweets = [t for t in items if not t.get('noResults')]
    print(f'Apify: {len(tweets)} real tweets (from {len(items)} items)')

    # Bucket by ticker
    buckets = {sym: [] for sym in UNIVERSE}
    for t in tweets:
        text = t.get('text') or t.get('full_text') or ''
        author_obj = t.get('author', {})
        if isinstance(author_obj, dict):
            author = (author_obj.get('userName')
                      or author_obj.get('screen_name', 'anon'))
        else:
            author = str(author_obj) or 'anon'
        likes = t.get('likeCount') or t.get('favorite_count') or 0
        for sym in UNIVERSE:
            if f'${sym}' in text.upper():
                buckets[sym].append({
                    'text':   text,
                    'score':  likes,
                    'author': author,
                    'source': 'x',
                })

    return buckets

# ── Scoring ───────────────────────────────────────────────────────────────────
def calc_ticker(sym, posts):
    if not posts:
        return None
    bull = bear = 0
    feed = []
    for p in posts:
        sent = score_sentiment(p['text'])
        if sent == 'Bullish':  bull += 1
        elif sent == 'Bearish': bear += 1
        feed.append({
            'body':      f"🐦 {p['text'][:140]}",
            'sentiment': sent,
            'user':      p.get('author', 'anon'),
            'followers': p.get('score', 0),
            'time':      '',
        })
    total      = bull + bear or 1
    bull_pct   = round(bull / total * 100)
    vol_score  = min(len(posts) / 10 * 40, 40)
    momo_score = min(round(vol_score + bull_pct * 0.6), 100)
    return {
        'sym':       sym,
        'name':      NAMES.get(sym, sym),
        'bullCount': bull,
        'bearCount': bear,
        'bullPct':   bull_pct,
        'bearPct':   100 - bull_pct,
        'total':     len(posts),
        'xPosts':    len(posts),
        'momoScore': momo_score,
        'posts':     sorted(feed, key=lambda p: p['followers'], reverse=True)[:5],
    }

# ── Background loop ───────────────────────────────────────────────────────────
def _refresh_loop():
    while True:
        try:
            buckets = fetch_all_tickers()
            if buckets:
                results = []
                for sym in UNIVERSE:
                    data = calc_ticker(sym, buckets.get(sym, []))
                    if data:
                        results.append(data)
                if results:
                    with _lock:
                        _cache['data'] = {
                            'stocks':    results,
                            'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                       time.gmtime()),
                            'sources':   ['X/Twitter'],
                        }
                        _cache['ts'] = time.time()
                    print(f'Cache updated: {len(results)} tickers from X')
                else:
                    print('Apify returned data but no tickers matched')
            else:
                print('Apify returned empty — cache unchanged')
        except Exception as e:
            print(f'Refresh loop error: {e}')
        time.sleep(REFRESH_INTERVAL)

threading.Thread(target=_refresh_loop, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────
@momo_bp.route('/api/momo')
def momo_index():
    with _lock:
        data = _cache['data']
    if not data:
        return jsonify({'error': 'Warming up — first X fetch in progress, check back in 2 min'}), 503
    return jsonify(data)

@momo_bp.route('/api/momo/ticker/<sym>')
def momo_ticker(sym):
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404
    with _lock:
        data = _cache['data']
    if not data:
        return jsonify({'error': 'Warming up'}), 503
    stocks = {s['sym']: s for s in data.get('stocks', [])}
    if sym not in stocks:
        return jsonify({'error': f'{sym} not in current cache'}), 404
    return jsonify(stocks[sym])
