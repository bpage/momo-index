"""
momo_api.py — MOMO Index backend
Uses yfinance for real price + volume momentum data.
No API key needed. Works from any cloud server.
"""

from flask import Blueprint, jsonify
import yfinance as yf
import time
import threading

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

# Simple in-memory cache to avoid hammering yfinance on every request
_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()
CACHE_TTL = 60  # seconds


def calc_momo(sym, hist):
    """Derive momentum score from 5-day OHLCV history."""
    try:
        if hist is None or len(hist) < 2:
            return None

        closes  = hist['Close'].dropna()
        volumes = hist['Volume'].dropna()
        if len(closes) < 2:
            return None

        current  = float(closes.iloc[-1])
        prev     = float(closes.iloc[-2])
        week_ago = float(closes.iloc[0])
        avg_vol  = float(volumes.mean())
        curr_vol = float(volumes.iloc[-1])

        pct_1d    = (current - prev)    / prev    * 100 if prev    else 0.0
        pct_5d    = (current - week_ago)/ week_ago* 100 if week_ago else 0.0
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0

        # Bull% : centred at 50, shifts ±5 pts per 1% daily move
        bull_pct = min(max(round(50 + pct_1d * 5), 0), 100)
        bear_pct = 100 - bull_pct

        # Momo score 0-100
        #   price component (0-60): 5-day return
        #   volume component (0-40): vol surge vs average
        price_score = min(max(pct_5d * 3 + 50, 0), 60)
        vol_score   = min(max((vol_ratio - 0.5) * 25, 0), 40)
        momo_score  = min(max(round(price_score + vol_score), 0), 100)

        # Scale volume to a "post count" proxy
        total_units = max(round(curr_vol / 1e5), 1)
        bull_count  = round(total_units * bull_pct / 100)
        bear_count  = total_units - bull_count

        direction = '▲' if pct_1d >= 0 else '▼'
        sentiment = 'Bullish' if pct_1d >= 0 else 'Bearish'
        posts = [{
            'body': (f"{sym} {direction} {pct_1d:+.2f}% today  |  "
                     f"5-day: {pct_5d:+.2f}%  |  "
                     f"Vol: {vol_ratio:.1f}x avg"),
            'sentiment': sentiment,
            'user': 'momo-engine',
            'followers': 0,
            'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }]

        return {
            'sym':       sym,
            'name':      NAMES.get(sym, sym),
            'bullCount': bull_count,
            'bearCount': bear_count,
            'bullPct':   bull_pct,
            'bearPct':   bear_pct,
            'total':     total_units,
            'momoScore': momo_score,
            'posts':     posts,
            'pct1d':     round(pct_1d, 2),
            'pct5d':     round(pct_5d, 2),
        }
    except Exception as e:
        print(f"Error processing {sym}: {e}")
        return None


def fetch_all():
    """Batch-download all tickers and compute momo scores."""
    raw = yf.download(
        UNIVERSE,
        period='5d',
        interval='1d',
        group_by='ticker',
        auto_adjust=True,
        threads=True,
        progress=False,
    )
    results = []
    for sym in UNIVERSE:
        try:
            hist = raw[sym] if len(UNIVERSE) > 1 else raw
        except Exception:
            continue
        data = calc_momo(sym, hist)
        if data:
            results.append(data)
    return results


@momo_bp.route('/api/momo')
def momo_index():
    """GET /api/momo — returns momentum scores for all tickers."""
    with _lock:
        if _cache['data'] and time.time() - _cache['ts'] < CACHE_TTL:
            return jsonify(_cache['data'])

    try:
        results = fetch_all()
    except Exception as e:
        print(f"fetch_all error: {e}")
        return jsonify({'error': 'Data unavailable'}), 503

    if not results:
        return jsonify({'error': 'Data unavailable'}), 503

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
    """GET /api/momo/ticker/NVDA — single ticker debug endpoint."""
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404
    try:
        hist = yf.Ticker(sym).history(period='5d', interval='1d')
    except Exception:
        return jsonify({'error': 'Failed to fetch'}), 503
    data = calc_momo(sym, hist)
    if not data:
        return jsonify({'error': 'Failed to process'}), 503
    return jsonify(data)
