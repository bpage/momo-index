"""
momo_api.py — MOMO Index backend
StockTwits sentiment, background-cached every 5 min.
Optional: set STOCKTWITS_TOKEN env var on Render for authenticated access.
"""

from flask import Blueprint, jsonify
import requests
import threading
import time
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

_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def fetch_stocktwits(sym):
    token  = os.environ.get('STOCKTWITS_TOKEN', '')
    params = {'limit': 30}
    if token:
        params['access_token'] = token

    try:
        res = requests.get(
            f'https://api.stocktwits.com/api/2/streams/symbol/{sym}.json',
            params=params,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=8,
        )
        if res.status_code != 200:
            print(f'StockTwits {res.status_code} for {sym}')
            return None

        messages = res.json().get('messages', [])
        bull = bear = 0
        posts = []

        for m in messages:
            sent = (m.get('entities') or {}).get('sentiment', {})
            label = sent.get('basic') if sent else None
            if label == 'Bullish':   bull += 1
            elif label == 'Bearish': bear += 1
            posts.append({
                'body':      m.get('body', '')[:140],
                'sentiment': label or 'Neutral',
                'user':      m.get('user', {}).get('username', 'trader'),
                'followers': m.get('user', {}).get('followers', 0),
                'time':      m.get('created_at', ''),
            })

        total     = bull + bear or 1
        bull_pct  = round(bull / total * 100)
        vol_score  = min(len(messages) / 30 * 40, 40)
        momo_score = round(vol_score + bull_pct * 0.6)

        return {
            'sym':       sym,
            'name':      NAMES.get(sym, sym),
            'bullCount': bull,
            'bearCount': bear,
            'bullPct':   bull_pct,
            'bearPct':   100 - bull_pct,
            'total':     len(messages),
            'momoScore': momo_score,
            'posts':     sorted(posts, key=lambda p: p['followers'], reverse=True)[:3],
        }
    except Exception as e:
        print(f'StockTwits error {sym}: {e}')
        return None


def _refresh_cache():
    results = []
    for sym in UNIVERSE:
        data = fetch_stocktwits(sym)
        if data:
            results.append(data)
        time.sleep(0.2)
    if not results:
        print('Cache refresh returned no results')
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


threading.Thread(target=_schedule_refresh, daemon=True).start()


@momo_bp.route('/api/momo')
def momo_index():
    with _lock:
        data = _cache['data']
    if not data:
        return jsonify({'error': 'Warming up — check back in 30 seconds'}), 503
    return jsonify(data)


@momo_bp.route('/api/momo/ticker/<sym>')
def momo_ticker(sym):
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404
    data = fetch_stocktwits(sym)
    if not data:
        return jsonify({'error': 'StockTwits unavailable'}), 503
    return jsonify(data)
