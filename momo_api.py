"""
momo_api.py — MOMO Index backend
Dual source: Reddit (every 5 min, free) + Apify/X Twitter (every 6h, ~$5/mo).
Background thread handles all refreshes; API responses are instant from cache.
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

# ── Reddit ────────────────────────────────────────────────────────────────────
MULTI_SUB = 'wallstreetbets+stocks+options+investing+stockmarket'
REDDIT_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/122.0.0.0 Safari/537.36'),
    'Accept': 'application/json',
}

# ── Apify / X ─────────────────────────────────────────────────────────────────
APIFY_TOKEN = os.environ.get('APIFY_TOKEN', '')
APIFY_ACTOR = 'apidojo~tweet-scraper'
APIFY_SYNC  = (f'https://api.apify.com/v2/acts/{APIFY_ACTOR}'
               f'/run-sync-get-dataset-items')
# One combined OR search for all tickers — one run, cheapest approach
X_SEARCH = ' OR '.join(f'${s}' for s in UNIVERSE)

# ── State ─────────────────────────────────────────────────────────────────────
_cache        = {'data': None, 'ts': 0}
_x_cache      = {}     # sym -> [post dicts]
_last_x_fetch = 0
_lock         = threading.Lock()

REDDIT_TTL = 300    # 5 min refresh
X_TTL      = 21600  # 6 hour refresh (keeps within $5/mo free tier)

# ── Helpers ───────────────────────────────────────────────────────────────────
def score_sentiment(text):
    words = set(re.sub(r'[^a-z\s]', '', text.lower()).split())
    bull  = len(words & BULL_WORDS)
    bear  = len(words & BEAR_WORDS)
    if bull > bear:  return 'Bullish'
    if bear > bull:  return 'Bearish'
    return 'Neutral'

def fetch_reddit(sym):
    try:
        url = (f'https://www.reddit.com/r/{MULTI_SUB}/search.json'
               f'?q=%24{sym}&restrict_sr=on&sort=new&t=day&limit=25')
        res = requests.get(url, headers=REDDIT_HEADERS, timeout=10)
        if res.status_code != 200:
            print(f'Reddit {res.status_code} for {sym}')
            return []
        posts = []
        for c in res.json().get('data', {}).get('children', []):
            d = c.get('data', {})
            posts.append({
                'text':   d.get('title', ''),
                'score':  d.get('score', 0),
                'author': d.get('author', 'anon'),
                'ts':     d.get('created_utc', 0),
                'sub':    d.get('subreddit', ''),
                'source': 'reddit',
            })
        return posts
    except Exception as e:
        print(f'Reddit error {sym}: {e}')
        return []

def fetch_x_apify():
    """One Apify run covering all 20 tickers. ~200 tweets ≈ $0.08."""
    if not APIFY_TOKEN:
        print('No APIFY_TOKEN — skipping X fetch')
        return {}
    try:
        payload = {
            'searchTerms':   [X_SEARCH],
            'maxItems':      200,
            'sort':          'Latest',
            'tweetLanguage': 'en',
        }
        print('Apify: starting X fetch for all tickers...')
        res = requests.post(
            APIFY_SYNC,
            params={'token': APIFY_TOKEN},
            json=payload,
            timeout=180,
        )
        if res.status_code != 200:
            print(f'Apify error {res.status_code}: {res.text[:300]}')
            return {}
        tweets = res.json()
        print(f'Apify: received {len(tweets)} tweets')

        # Bucket each tweet by which tickers it mentions
        result = {sym: [] for sym in UNIVERSE}
        for t in tweets:
            text = (t.get('text') or t.get('full_text') or '')
            author_obj = t.get('author', {})
            if isinstance(author_obj, dict):
                author = (author_obj.get('userName')
                          or author_obj.get('screen_name', 'anon'))
            else:
                author = str(author_obj) or 'anon'
            likes = (t.get('likeCount') or t.get('favorite_count') or 0)
            for sym in UNIVERSE:
                if f'${sym}' in text.upper():
                    result[sym].append({
                        'text':   text,
                        'score':  likes,
                        'author': author,
                        'ts':     0,
                        'source': 'x',
                    })
        return result
    except Exception as e:
        print(f'Apify error: {e}')
        return {}

def calc_ticker(sym, reddit_posts, x_posts=None):
    all_posts = list(reddit_posts) + list(x_posts or [])
    if not all_posts:
        return None

    bull_count = bear_count = 0
    feed_posts = []
    for p in all_posts:
        sent = score_sentiment(p['text'])
        if sent == 'Bullish':  bull_count += 1
        elif sent == 'Bearish': bear_count += 1
        prefix = '🐦' if p.get('source') == 'x' else f"[r/{p.get('sub','?')}]"
        feed_posts.append({
            'body':      f"{prefix} {p['text'][:120]}",
            'sentiment': sent,
            'user':      p.get('author', 'anon'),
            'followers': p.get('score', 0),
            'time':      time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                       time.gmtime(p.get('ts') or 0)),
        })

    total    = bull_count + bear_count or 1
    bull_pct = round(bull_count / total * 100)
    vol      = len(all_posts)
    x_count  = sum(1 for p in all_posts if p.get('source') == 'x')

    vol_score  = min(vol / 25 * 40, 40)
    momo_score = min(round(vol_score + bull_pct * 0.6), 100)

    return {
        'sym':       sym,
        'name':      NAMES.get(sym, sym),
        'bullCount': bull_count,
        'bearCount': bear_count,
        'bullPct':   bull_pct,
        'bearPct':   100 - bull_pct,
        'total':     vol,
        'xPosts':    x_count,
        'momoScore': momo_score,
        'posts':     sorted(feed_posts,
                            key=lambda p: p['followers'],
                            reverse=True)[:5],
    }

# ── Background refresh loops ──────────────────────────────────────────────────

def _reddit_loop():
    """Refresh Reddit data every 5 min. Always runs; never waits on Apify."""
    while True:
        try:
            with _lock:
                x_data = dict(_x_cache)
            results = []
            for sym in UNIVERSE:
                posts = fetch_reddit(sym)
                data  = calc_ticker(sym, posts, x_data.get(sym, []))
                if data:
                    results.append(data)
                time.sleep(2)   # 2s gap — gentler on Reddit to avoid 429s

            if results:
                sources = ['Reddit']
                if x_data and any(x_data.values()):
                    sources.append('X/Twitter')
                with _lock:
                    _cache['data'] = {
                        'stocks':    results,
                        'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                   time.gmtime()),
                        'sources':   sources,
                    }
                    _cache['ts'] = time.time()
                print(f'Reddit cache: {len(results)} tickers | {sources}')
            else:
                print('Reddit cache: no results this cycle')
        except Exception as e:
            print(f'Reddit loop error: {e}')
        time.sleep(REDDIT_TTL)


def _apify_loop():
    """Refresh X/Twitter data every 6h via Apify — runs independently."""
    # Wait 30s on boot so Reddit populates the cache first
    time.sleep(30)
    while True:
        try:
            fresh = fetch_x_apify()
            if fresh:
                with _lock:
                    _x_cache.update(fresh)
                print('Apify X cache updated')
        except Exception as e:
            print(f'Apify loop error: {e}')
        time.sleep(X_TTL)


threading.Thread(target=_reddit_loop, daemon=True).start()
if APIFY_TOKEN:
    threading.Thread(target=_apify_loop, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────
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
    reddit_posts = fetch_reddit(sym)
    with _lock:
        x_posts = _x_cache.get(sym, [])
    data = calc_ticker(sym, reddit_posts, x_posts)
    if not data:
        return jsonify({'error': 'No posts found'}), 503
    return jsonify(data)
