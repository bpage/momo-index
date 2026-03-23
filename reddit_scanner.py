"""
reddit_scanner.py — Reddit cashtag scanner for MOMO INDEX
Uses Reddit's public JSON API (no credentials required).
Scans r/wallstreetbets, r/stocks, r/investing, r/options for $TICKER mentions.
Scores by upvotes, comments, and recency decay.
"""

import re
import requests
import time
import math
import logging
from collections import defaultdict

log = logging.getLogger(__name__)

SUBREDDITS = ['wallstreetbets', 'stocks', 'investing', 'options']
SUBREDDIT_WEIGHTS = {
    'wallstreetbets': 1.5,
    'stocks': 1.0,
    'investing': 0.8,
    'options': 1.2,
}

HEADERS = {
    'User-Agent': 'MomoIndex/1.0 (social scanner; contact: admin@mrpage.com)',
    'Accept': 'application/json',
}

_REQUEST_DELAY = 1.5  # seconds between Reddit requests to respect rate limits


def _recency_decay(created_utc: float, half_life_hours: float = 6.0) -> float:
    """Exponential decay based on post age. Half-life = 6 hours."""
    age_hours = (time.time() - created_utc) / 3600
    return math.exp(-0.693 * age_hours / half_life_hours)


def _score_post(upvotes: int, comments: int, created_utc: float) -> float:
    """
    Engagement score formula:
      base = upvotes×1 + comments×2  (comments weighted more — signal of discussion)
      score = base × recency_decay(6h half-life)
    """
    base = max(upvotes, 0) * 1.0 + comments * 2.0
    return base * _recency_decay(created_utc)


def _extract_cashtags(text: str, universe: set) -> list:
    """Extract $TICKER mentions that exist in our universe."""
    raw = re.findall(r'\$([A-Z]{1,5})', text.upper())
    return [t for t in raw if t in universe]


def fetch_reddit_scores(universe: list, lookback_hours: int = 24) -> dict:
    """
    Scan Reddit for cashtag mentions across SUBREDDITS.

    Args:
        universe: list of ticker symbols to track
        lookback_hours: how many hours back to scan (used as age cutoff)

    Returns:
        dict {sym: normalized_score} where score is 0–100
    """
    universe_set = set(universe)
    cutoff = time.time() - (lookback_hours * 3600)
    raw_scores = defaultdict(float)

    for subreddit in SUBREDDITS:
        weight = SUBREDDIT_WEIGHTS.get(subreddit, 1.0)
        try:
            # Fetch both hot and rising — dedupe by id so a post isn't double-counted
            seen_ids: dict = {}
            for feed in ('hot', 'rising'):
                url = f"https://www.reddit.com/r/{subreddit}/{feed}.json?limit=100"
                resp = requests.get(url, headers=HEADERS, timeout=10)
                if resp.status_code == 429:
                    log.warning(f"[reddit] Rate limited on r/{subreddit}/{feed}, skipping feed")
                    time.sleep(_REQUEST_DELAY * 3)
                    continue
                if resp.status_code != 200:
                    log.warning(f"[reddit] r/{subreddit}/{feed} returned {resp.status_code}")
                    time.sleep(_REQUEST_DELAY)
                    continue
                for post_wrap in resp.json().get('data', {}).get('children', []):
                    pid = post_wrap.get('data', {}).get('id')
                    if pid and pid not in seen_ids:
                        seen_ids[pid] = post_wrap
                time.sleep(_REQUEST_DELAY)

            for post_wrap in seen_ids.values():
                post = post_wrap.get('data', {})
                created_utc = float(post.get('created_utc', 0))
                if created_utc < cutoff:
                    continue  # too old

                title = post.get('title', '')
                selftext = post.get('selftext', '')
                full_text = f"{title} {selftext}"
                upvotes = int(post.get('ups', 0))
                comments = int(post.get('num_comments', 0))

                tickers = _extract_cashtags(full_text, universe_set)
                if not tickers:
                    # Also check title for naked symbols in WSB-style posts
                    bare = re.findall(r'\b([A-Z]{2,5})\b', title.upper())
                    tickers = [t for t in bare if t in universe_set]

                post_score = _score_post(upvotes, comments, created_utc)

                for sym in set(tickers):  # dedupe per post
                    raw_scores[sym] += post_score * weight

        except Exception as e:
            log.error(f"[reddit] Error scanning r/{subreddit}: {e}")
            time.sleep(_REQUEST_DELAY)

    if not raw_scores:
        log.info("[reddit] No signals found this scan")
        return {}

    # Log-normalize to 0–100: compresses dominant tickers, lifts low-volume ones
    log_scores = {sym: math.log1p(score) for sym, score in raw_scores.items()}
    max_log = max(log_scores.values()) or 1
    normalized = {
        sym: round(min(log_scores[sym] / max_log * 100, 100), 1)
        for sym in raw_scores
        if sym in universe_set
    }

    log.info(
        f"[reddit] Scan complete. {len(normalized)} tickers found. "
        f"Top: {sorted(normalized.items(), key=lambda x: -x[1])[:5]}"
    )
    return normalized


def get_reddit_signal(universe: list) -> dict:
    """
    Public interface for MOMO pipeline.
    Returns {sym: score_0_to_100} for tickers in universe.
    Empty dict on failure.
    """
    try:
        return fetch_reddit_scores(universe)
    except Exception as e:
        log.error(f"[reddit] get_reddit_signal failed: {e}")
        return {}
