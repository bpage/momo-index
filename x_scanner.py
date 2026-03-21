"""
x_scanner.py — X (Twitter) cashtag scanner for MOMO INDEX
Requires X API v2 Bearer Token in env var X_BEARER_TOKEN.
Gracefully returns empty dict when token is absent.

To enable:
  1. Create a free app at developer.x.com
  2. Copy the Bearer Token
  3. Add X_BEARER_TOKEN to Render env vars for covered-calls-bot
"""

import os
import time
import math
import logging
import requests
from collections import defaultdict

log = logging.getLogger(__name__)

BEARER_TOKEN = os.environ.get('X_BEARER_TOKEN', '')

X_SEARCH_URL = 'https://api.twitter.com/2/tweets/search/recent'

# Max results per request (100 = max for Basic tier)
MAX_RESULTS = 100


def _auth_headers() -> dict:
    return {'Authorization': f'Bearer {BEARER_TOKEN}'}


def _recency_decay(created_at: str, half_life_hours: float = 6.0) -> float:
    """Exponential decay. created_at is ISO8601 string from X API."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return math.exp(-0.693 * age_hours / half_life_hours)
    except Exception:
        return 0.5  # neutral if parse fails


def _score_tweet(likes: int, retweets: int, replies: int, quotes: int, created_at: str) -> float:
    """
    Engagement score:
      base = likes×1 + retweets×2 + replies×1.5 + quotes×1.5
      score = base × recency_decay(6h half-life)
    """
    base = likes * 1.0 + retweets * 2.0 + replies * 1.5 + quotes * 1.5
    # Give at least 1 point for any tweet (presence signal)
    base = max(base, 1.0)
    return base * _recency_decay(created_at)


def fetch_x_scores(universe: list, lookback_hours: int = 24) -> dict:
    """
    Search X for cashtag mentions across all universe tickers.
    Uses a single broad search then filters client-side to avoid many requests.

    Returns:
        dict {sym: normalized_score} where score is 0–100
        Empty dict if X_BEARER_TOKEN not set or API call fails.
    """
    if not BEARER_TOKEN:
        log.info("[x] X_BEARER_TOKEN not set — skipping X scanner")
        return {}

    universe_set = set(universe)
    raw_scores = defaultdict(float)

    # Build a broad query: cashtags in English, no retweets, last 24h
    # X API basic search covers last 7 days; we'll filter by time ourselves
    query = 'has:cashtags lang:en -is:retweet'
    cutoff_iso = time.strftime(
        '%Y-%m-%dT%H:%M:%SZ',
        time.gmtime(time.time() - lookback_hours * 3600)
    )

    params = {
        'query': query,
        'max_results': MAX_RESULTS,
        'start_time': cutoff_iso,
        'tweet.fields': 'created_at,public_metrics,entities',
        'expansions': '',
    }

    try:
        resp = requests.get(
            X_SEARCH_URL,
            headers=_auth_headers(),
            params=params,
            timeout=15,
        )

        if resp.status_code == 401:
            log.warning("[x] Invalid Bearer Token — check X_BEARER_TOKEN env var")
            return {}
        if resp.status_code == 403:
            log.warning("[x] X API access forbidden (check app permissions/tier)")
            return {}
        if resp.status_code == 429:
            log.warning("[x] X API rate limited — will retry next scan cycle")
            return {}
        if resp.status_code != 200:
            log.warning(f"[x] X API returned {resp.status_code}: {resp.text[:200]}")
            return {}

        data = resp.json()
        tweets = data.get('data', [])

        if not tweets:
            log.info("[x] No tweets found matching query")
            return {}

        for tweet in tweets:
            # Extract cashtags from entities (API-native, most reliable)
            entities = tweet.get('entities') or {}
            cashtags_raw = entities.get('cashtags', []) or []
            tickers = [ct['tag'].upper() for ct in cashtags_raw if ct.get('tag', '').upper() in universe_set]

            if not tickers:
                continue

            metrics = tweet.get('public_metrics') or {}
            likes    = int(metrics.get('like_count', 0))
            retweets = int(metrics.get('retweet_count', 0))
            replies  = int(metrics.get('reply_count', 0))
            quotes   = int(metrics.get('quote_count', 0))
            created_at = tweet.get('created_at', '')

            tweet_score = _score_tweet(likes, retweets, replies, quotes, created_at)

            for sym in set(tickers):
                raw_scores[sym] += tweet_score

    except Exception as e:
        log.error(f"[x] Error fetching X data: {e}")
        return {}

    if not raw_scores:
        return {}

    # Normalize to 0–100
    max_score = max(raw_scores.values()) or 1
    normalized = {
        sym: round(min(score / max_score * 100, 100), 1)
        for sym, score in raw_scores.items()
        if sym in universe_set
    }

    log.info(
        f"[x] Scan complete. {len(normalized)} tickers found. "
        f"Top: {sorted(normalized.items(), key=lambda x: -x[1])[:5]}"
    )
    return normalized


def get_x_signal(universe: list) -> dict:
    """
    Public interface for MOMO pipeline.
    Returns {sym: score_0_to_100} for tickers in universe.
    Empty dict on failure or missing credentials.
    """
    try:
        return fetch_x_scores(universe)
    except Exception as e:
        log.error(f"[x] get_x_signal failed: {e}")
        return {}
