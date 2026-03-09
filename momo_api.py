"""
momo_api.py — MOMO Index backend
Fetches real StockTwits data server-side (no CORS issues).
Add to your existing Render repo alongside your covered calls bot.
"""

from flask import Blueprint, jsonify
import requests
import time

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
                'time': m.get('created_at', '')
            })

        total = bull_count + bear_count or 1
        bull_pct = round(bull_count / total * 100)
        bear_pct = 100 - bull_pct

        # Momo score formula: volume surge + sentiment strength
        vol_score = min(len(messages) / 30 * 40, 40)
        sent_score = bull_pct * 0.6
        momo_score = round(vol_score + sent_score)

        return {
            'sym': sym,
            'name': NAMES.get(sym, sym),
            'bullCount': bull_count,
            'bearCount': bear_count,
            'bullPct': bull_pct,
            'bearPct': bear_pct,
            'total': len(messages),
            'momoScore': momo_score,
            'posts': posts[:3],
        }
    except Exception as e:
        print(f"Error fetching {sym}: {e}")
        return None


@momo_bp.route('/api/momo')
def momo_index():
    """
    GET /api/momo
    Returns sentiment + momo scores for all tickers.
    Called by the dashboard every 90 seconds.
    """
    results = []
    for sym in UNIVERSE:
        data = fetch_stocktwits(sym)
        if data:
            results.append(data)
        time.sleep(0.15)  # be polite to StockTwits rate limits

    if not results:
        return jsonify({'error': 'StockTwits unavailable'}), 503

    return jsonify({
        'stocks': results,
        'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
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
    data = fetch_stocktwits(sym)
    if not data:
        return jsonify({'error': 'Failed to fetch'}), 503
    return jsonify(data)
