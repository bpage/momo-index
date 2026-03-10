"""
momo_api.py — MOMO Index backend
Data is pushed by the morning X-browse scheduled task (no background scraping).
"""

from flask import Blueprint, jsonify, request
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

PUSH_KEY = os.environ.get('MOMO_PUSH_KEY', '')

# ── State ─────────────────────────────────────────────────────────────────────
_cache = {'data': None, 'ts': 0}
_lock  = threading.Lock()

# ── Routes ────────────────────────────────────────────────────────────────────
@momo_bp.route('/api/momo')
def momo_index():
    with _lock:
        data = _cache['data']
    if not data:
        return jsonify({'error': 'No data yet — morning X scan not run today'}), 503
    return jsonify(data)

@momo_bp.route('/api/momo/ticker/<sym>')
def momo_ticker(sym):
    sym = sym.upper()
    if sym not in UNIVERSE:
        return jsonify({'error': 'Ticker not in universe'}), 404
    with _lock:
        data = _cache['data']
    if not data:
        return jsonify({'error': 'No data yet'}), 503
    stocks = {s['sym']: s for s in data.get('stocks', [])}
    if sym not in stocks:
        return jsonify({'error': f'{sym} not in current cache'}), 404
    return jsonify(stocks[sym])

@momo_bp.route('/api/momo/push', methods=['POST'])
def momo_push():
    """Receive sentiment data pushed by the morning X-browse task."""
    body = request.get_json(force=True, silent=True) or {}

    if not PUSH_KEY or body.get('key') != PUSH_KEY:
        return jsonify({'error': 'Unauthorized'}), 401

    data = body.get('data') or {}
    stocks = data.get('stocks') or []
    if not stocks:
        return jsonify({'error': 'No stocks in payload'}), 400

    with _lock:
        _cache['data'] = {
            'stocks':    stocks,
            'fetchedAt': data.get('fetchedAt', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())),
            'sources':   data.get('sources', ['X/Twitter']),
        }
        _cache['ts'] = time.time()

    print(f'Push received: {len(stocks)} tickers at {_cache["data"]["fetchedAt"]}')
    return jsonify({'ok': True, 'tickers': len(stocks)})

@momo_bp.route('/api/momo/status')
def momo_status():
    with _lock:
        ts  = _cache['ts']
        has = _cache['data'] is not None
    age_min = round((time.time() - ts) / 60) if ts else None
    return jsonify({'hasData': has, 'ageMinutes': age_min})
