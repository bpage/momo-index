from flask import Blueprint, jsonify
import requests, time

momo_bp = Blueprint('momo', __name__)

UNIVERSE = ['NVDA','TSLA','AAPL','META','AMZN','MSFT','GOOGL','AMD','COIN','MSTR','HOOD','PLTR','SOFI','SMCI','RIVN','SPOT','CRWD','MELI','SHOP','SPY']
NAMES = {'NVDA':'Nvidia','TSLA':'Tesla','AAPL':'Apple','META':'Meta','AMZN':'Amazon','MSFT':'Microsoft','GOOGL':'Alphabet','AMD':'AMD','COIN':'Coinbase','MSTR':'MicroStrategy','HOOD':'Robinhood','PLTR':'Palantir','SOFI':'SoFi','SMCI':'Supermicro','RIVN':'Rivian','SPOT':'Spotify','CRWD':'CrowdStrike','MELI':'MercadoLibre','SHOP':'Shopify','SPY':'S&P 500 ETF'}

def fetch_stocktwits(sym):
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json?limit=30"
    try:
        res = requests.get(url, timeout=8, headers={'User-Agent':'Mozilla/5.0'})
        if res.status_code != 200: return None
        messages = res.json().get('messages', [])
        bull, bear = 0, 0
        posts = []
        for m in messages:
            sent = (m.get('entities') or {}).get('sentiment', {})
            s = sent.get('basic') if sent else None
            if s == 'Bullish': bull += 1
            elif s == 'Bearish': bear += 1
            posts.append({'body': m.get('body','')[:140], 'sentiment': s or 'Neutral', 'user': m.get('user',{}).get('username','trader'), 'followers': m.get('user',{}).get('followers',0), 'time': m.get('created_at','')})
        total = bull + bear or 1
        bull_pct = round(bull / total * 100)
        momo = round(min(len(messages)/30*40, 40) + bull_pct * 0.6)
        return {'sym':sym,'name':NAMES.get(sym,sym),'bullCount':bull,'bearCount':bear,'bullPct':bull_pct,'bearPct':100-bull_pct,'total':len(messages),'momoScore':momo,'posts':posts[:3]}
    except Exception as e:
        print(f"Error {sym}: {e}")
        return None

@momo_bp.route('/api/momo')
def momo_index():
    results = []
    for sym in UNIVERSE:
        d = fetch_stocktwits(sym)
        if d: results.append(d)
        time.sleep(0.15)
    if not results: return jsonify({'error':'unavailable'}), 503
    return jsonify({'stocks': results, 'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
