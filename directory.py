"""Apple Podcasts directory gateway: search and top charts.

Depends on protocol (HTTP settings, logging, keys) and store (show cache).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from protocol import HTTP_TIMEOUT, USER_AGENT, clean_key, log
from store import cache_show

GENRES = [
    ("1489", "News"),
    ("1318", "Technology"),
    ("1488", "True Crime"),
    ("1303", "Comedy"),
    ("1324", "Society & Culture"),
    ("1487", "History"),
    ("1533", "Science"),
    ("1512", "Health & Fitness"),
    ("1321", "Business"),
    ("1310", "Music"),
    ("1545", "Sports"),
    ("1301", "Arts"),
    ("1304", "Education"),
    ("1483", "Fiction"),
    ("1309", "TV & Film"),
    ("1314", "Religion & Spirituality"),
    ("1305", "Kids & Family"),
    ("1502", "Leisure"),
    ("1511", "Government"),
]


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as e:
        log(f"fetch_json failed for {url}: {e}")
        return None


def apple_search(term, genre_id=None):
    params = {"term": term, "media": "podcast", "entity": "podcast", "limit": "100"}
    if genre_id:
        params["genreId"] = str(genre_id)
    url = f"https://itunes.apple.com/search?{urllib.parse.urlencode(params)}"
    data = fetch_json(url) or {}
    results = []
    for item in data.get("results", []):
        feed_url = item.get("feedUrl")
        if not feed_url:
            continue
        show = {
            "id": f"show:{clean_key(feed_url)}",
            "feed_url": feed_url,
            "title": item.get("collectionName", "Untitled Show"),
            "author": item.get("artistName", ""),
            "artwork": item.get("artworkUrl600") or item.get("artworkUrl100", ""),
            "genre": item.get("primaryGenreName", ""),
            "episodes": item.get("trackCount", 0),
        }
        cache_show(
            feed_url,
            show["title"],
            show["author"],
            show["artwork"],
            show["genre"],
            show["episodes"],
        )
        results.append(show)
    return results


def normalize_country(country):
    """Two-letter lowercase country code, defaulting to us like cliamp."""
    country = (country or "").strip().lower()
    if len(country) != 2 or not country.isalpha():
        return "us"
    return country


def apple_top_charts(country):
    country = normalize_country(country)
    url = (
        f"https://rss.marketingtools.apple.com/api/v2/"
        f"{country}/podcasts/top/100/podcasts.json"
    )
    data = fetch_json(url)
    if not data:
        return []
    try:
        entries = data["feed"]["results"]
    except (KeyError, TypeError):
        log(f"top charts for {country}: missing feed.results")
        return []
    ids = []
    for entry in entries:
        if not isinstance(entry, dict):
            log(f"top charts for {country}: null result entry")
            return []
        chart_id = (entry.get("id") or "").strip()
        if chart_id:
            ids.append(chart_id)
    if not ids:
        return []

    # Batched lookup
    lookup_url = (
        f"https://itunes.apple.com/lookup?id={','.join(ids[:100])}&entity=podcast"
    )
    l_data = fetch_json(lookup_url) or {}
    show_map = {}
    for item in l_data.get("results", []):
        feed_url = item.get("feedUrl")
        if feed_url:
            show = {
                "id": f"show:{clean_key(feed_url)}",
                "feed_url": feed_url,
                "title": item.get("collectionName", "Untitled"),
                "author": item.get("artistName", ""),
                "artwork": item.get("artworkUrl600") or item.get("artworkUrl100", ""),
                "genre": item.get("primaryGenreName", ""),
                "episodes": item.get("trackCount", 0),
            }
            cache_show(
                feed_url,
                show["title"],
                show["author"],
                show["artwork"],
                show["genre"],
                show["episodes"],
            )
            show_map[str(item.get("collectionId"))] = show
    # Preserve rank order, deduplicated by feed URL.
    ranked = []
    seen_feeds = set()
    for chart_id in ids:
        show = show_map.get(chart_id)
        if show and show["feed_url"] not in seen_feeds:
            seen_feeds.add(show["feed_url"])
            ranked.append(show)
    return ranked
