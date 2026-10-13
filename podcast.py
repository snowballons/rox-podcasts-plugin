#!/usr/bin/env python3
"""Rox Player Podcast Plugin (API 1).

Implements Apple Podcasts directory discovery, publisher RSS parsing,
HTTP range streaming, native chapters, and library synchronization.
"""

import base64
import concurrent.futures
import email.utils
import hashlib
import http.client
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

API = 1
VERSION = "1.0.0"
USER_AGENT = f"RoxPodcastPlugin/{VERSION} (+https://github.com/zealsprince/rox)"
HTTP_TIMEOUT = 12
CHUNK_CAP = 256 * 1024
PAGE_SIZE = 30

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
]

# Thread safety & global state
out_lock = threading.Lock()
state = {
    "data_dir": None,
    "country": "us",
    "max_episodes": 300,
    "features": set(),
}

db_conn = None
db_lock = threading.Lock()
fan = concurrent.futures.ThreadPoolExecutor(max_workers=8)

# ── JSON-RPC Transport ─────────────────────────────────────────────

def send(frame):
    payload = (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    with out_lock:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()

def log(text):
    sys.stderr.write(f"{str(text)[:4000]}\n")
    sys.stderr.flush()

class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

# ── Database & Persistence ─────────────────────────────────────────

def init_db(data_dir):
    global db_conn
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "podcasts.db")
    db_conn = sqlite3.connect(db_path, check_same_thread=False)
    with db_lock:
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                feed_url TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                artwork TEXT,
                genre TEXT,
                subscribed_at INTEGER
            )
        """)
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS shows (
                feed_url TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                artwork TEXT,
                genre TEXT,
                episode_count INTEGER,
                cached_at INTEGER
            )
        """)
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_cache (
                feed_url TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                xml_data TEXT,
                fetched_at INTEGER
            )
        """)
        db_conn.commit()

def is_subscribed(feed_url):
    with db_lock:
        row = db_conn.execute("SELECT 1 FROM subscriptions WHERE feed_url = ?", (feed_url,)).fetchone()
        return row is not None

def add_subscription(feed_url, title="", author="", artwork="", genre=""):
    with db_lock:
        db_conn.execute("""
            INSERT OR REPLACE INTO subscriptions (feed_url, title, author, artwork, genre, subscribed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (feed_url, title, author, artwork, genre, int(time.time())))
        db_conn.commit()

def remove_subscription(feed_url):
    with db_lock:
        db_conn.execute("DELETE FROM subscriptions WHERE feed_url = ?", (feed_url,))
        db_conn.commit()

def get_subscriptions():
    with db_lock:
        cur = db_conn.execute("SELECT feed_url, title, author, artwork, genre FROM subscriptions ORDER BY subscribed_at DESC")
        return cur.fetchall()

def cache_show(feed_url, title="", author="", artwork="", genre="", episode_count=0):
    """Cache show metadata so browse/open can resolve feed URLs without a live subscription."""
    with db_lock:
        db_conn.execute("""
            INSERT OR REPLACE INTO shows (feed_url, title, author, artwork, genre, episode_count, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (feed_url, title, author, artwork, genre, episode_count, int(time.time())))
        db_conn.commit()

def get_show(feed_url):
    """Return cached show metadata (title, author, artwork, genre) or None."""
    with db_lock:
        row = db_conn.execute(
            "SELECT title, author, artwork, genre FROM shows WHERE feed_url = ?", (feed_url,)
        ).fetchone()
        return row

def get_show_by_key(key):
    """Resolve a show node key (sha256[:24] of feed_url) to its feed_url, or None."""
    with db_lock:
        rows = db_conn.execute("SELECT feed_url FROM shows").fetchall()
        for (feed_url,) in rows:
            if clean_key(feed_url) == key:
                return (feed_url,)
        return None


# ── Helpers & Parsers ──────────────────────────────────────────────

def parse_duration_ms(raw):
    if not raw:
        return 0
    s = raw.strip()
    if ":" not in s:
        try:
            return max(0, int(float(s) * 1000))
        except ValueError:
            return 0
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))) * 1000
        if len(parts) == 2:
            return (int(parts[0]) * 60 + int(float(parts[1]))) * 1000
    except ValueError:
        return 0
    return 0

def parse_pub_date(raw):
    if not raw:
        return 0
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.year
    except (TypeError, ValueError, OverflowError):
        return 0

def clean_key(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

# ── Apple Directory Gateway ────────────────────────────────────────

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as e:
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
        cache_show(feed_url, show["title"], show["author"], show["artwork"], show["genre"], show["episodes"])
        results.append(show)
    return results

def apple_top_charts(country):
    url = f"https://itunes.apple.com/{country.lower()}/rss/toppodcasts/limit=50/json"
    data = fetch_json(url)
    if not data or "feed" not in data or "entry" not in data["feed"]:
        return []
    entries = data["feed"]["entry"]
    ids = []
    for e in entries:
        im_id = e.get("id", {}).get("attributes", {}).get("im:id")
        if im_id:
            ids.append(im_id)
    if not ids:
        return []

    # Batched lookup
    lookup_url = f"https://itunes.apple.com/lookup?id={','.join(ids[:50])}&entity=podcast"
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
            cache_show(feed_url, show["title"], show["author"], show["artwork"], show["genre"], show["episodes"])
            show_map[str(item.get("collectionId"))] = show
    # Preserve rank order
    return [show_map[i] for i in ids if i in show_map]

# ── RSS Parser Engine ──────────────────────────────────────────────

def parse_feed(feed_url):
    req = urllib.request.Request(feed_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            content = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        raise Failure(-32000, f"Failed to fetch RSS: {e}")

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise Failure(-32000, f"Malformed XML feed: {e}")

    channel = root.find("channel")
    if channel is None:
        raise Failure(-32000, "Missing RSS channel")

    ns = {
        "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "psc": "http://podlove.org/simple-chapters",
    }
    show_title = (channel.findtext("title") or "Untitled Show").strip()
    show_author = (channel.findtext("itunes:author", namespaces=ns) or channel.findtext("author") or "").strip()
    show_image = ""
    img_node = channel.find("itunes:image", namespaces=ns)
    if img_node is not None and "href" in img_node.attrib:
        show_image = img_node.attrib["href"]
    if not show_image:
        img_node = channel.find("image")
        if img_node is not None:
            show_image = (img_node.findtext("url") or "").strip()

    episodes = []
    items = channel.findall("item")
    limit = state["max_episodes"]

    for idx, item in enumerate(items[:limit]):
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        audio_url = enclosure.attrib.get("url", "").strip()
        enc_type = enclosure.attrib.get("type", "").lower()

        # Audio mime or extension check
        if not (enc_type.startswith("audio/") or audio_url.endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus"))):
            continue

        guid = item.findtext("guid") or audio_url
        title = (item.findtext("title") or "Untitled Episode").strip()
        duration_raw = item.findtext("itunes:duration", namespaces=ns) or ""
        duration_ms = parse_duration_ms(duration_raw)
        pub_year = parse_pub_date(item.findtext("pubDate"))

        # Episode-specific chapters
        chapters = []
        psc_chapters = item.findall("psc:chapters/psc:chapter", namespaces=ns)
        for c in psc_chapters:
            start_ms = parse_duration_ms(c.attrib.get("start", ""))
            c_title = c.attrib.get("title", "").strip()
            if c_title:
                chapters.append({"start_ms": start_ms, "title": c_title})

        ep_key = f"ep:{clean_key(feed_url)}:{clean_key(guid)}"

        track_entry = {
            "key": ep_key,
            "title": title,
            "artist": show_author or show_title,
            "album_artist": show_author or show_title,
            "album": show_title,
            "genre": "Podcast",
            "year": pub_year,
            "disc_no": 1,
            "track_no": idx + 1,
            "duration_ms": duration_ms,
            "codec": "MP3" if "mpeg" in enc_type or audio_url.endswith(".mp3") else "AAC",
            "bitrate_kbps": 128,
            "live": False,
            "values": {},
        }

        episodes.append({
            "track": track_entry,
            "audio_url": audio_url,
            "chapters": chapters,
        })

    return {
        "title": show_title,
        "author": show_author,
        "artwork": show_image,
        "episodes": episodes,
    }

# ── Streaming Proxy Engine (source.open / read / close) ────────────

class StreamInstance:
    def __init__(self, url, length, chapters):
        self.origin_url = url
        self.resolved_url = url
        self.length = length
        self.chapters = chapters
        self.lock = threading.Lock()
        self.conn = None
        self.origin_tuple = None

    def close(self):
        with self.lock:
            if self.conn:
                try:
                    self.conn.close()
                except OSError as e:
                    log(f"stream close error: {e}")
            self.conn = None

    def read_range(self, start, end):
        with self.lock:
            for _ in range(5):  # Follow up to 5 HTTP 3xx redirects
                parts = urllib.parse.urlsplit(self.resolved_url)
                origin = (parts.scheme, parts.hostname, parts.port)
                if self.conn is None or origin != self.origin_tuple:
                    if self.conn:
                        try:
                            self.conn.close()
                        except OSError as e:
                            log(f"stream close error: {e}")
                    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
                    self.conn = cls(parts.hostname, parts.port, timeout=HTTP_TIMEOUT)
                    self.origin_tuple = origin

                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                headers = {"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}

                try:
                    self.conn.request("GET", path, headers=headers)
                    resp = self.conn.getresponse()

                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.getheader("Location")
                        resp.read()
                        self.resolved_url = urllib.parse.urljoin(self.resolved_url, loc)
                        continue

                    if resp.status not in (200, 206):
                        raise Failure(-32000, f"Upstream HTTP error: {resp.status}")

                    data = resp.read()
                    return data
                except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                    if self.conn:
                        try:
                            self.conn.close()
                        except OSError as e2:
                            log(f"stream close error: {e2}")
                        self.conn = None
                    raise Failure(-32000, f"Stream read error: {e}")

            raise Failure(-32000, "Too many redirects")

streams = {}
streams_lock = threading.Lock()
stream_counter = 0

# Ep cache: map track key -> (audio_url, chapters, artwork)
ep_metadata_cache = {}

# ── JSON-RPC Method Handlers ───────────────────────────────────────

def rpc_hello(params):
    state["data_dir"] = params.get("data_dir")
    state["features"] = set(params.get("features") or [])
    cfg = params.get("config") or {}
    if "country" in cfg:
        state["country"] = cfg["country"]
    if "max_episodes" in cfg:
        state["max_episodes"] = cfg["max_episodes"]

    init_db(state["data_dir"])
    return {"name": "Podcasts", "version": VERSION, "api": API}

def rpc_browse(params):
    node = params.get("node")

    # 1. Roots
    if node is None:
        entries = [
            {"node": {"id": "home", "title": "Home", "subtitle": "Top charts & subscriptions", "collection": False, "home": True, "kind":
"folder"}},
            {"node": {"id": "subs", "title": "Subscriptions", "subtitle": "Your saved shows", "collection": False, "kind": "folder"}},
            {"node": {"id": "charts", "title": "Top Charts", "subtitle": f"Top shows in {state['country'].upper()}", "collection": False, "kind":
"folder"}},
            {"node": {"id": "categories", "title": "Categories", "subtitle": "Browse by genre", "collection": False, "kind": "folder"}},
        ]
        return {"entries": entries, "cursor": None}

    # 2. Home view
    if node == "home":
        sections = []
        # Subscriptions Shelf
        subs = get_subscriptions()
        if subs:
            sub_nodes = [{
                "id": f"show:{clean_key(f[0])}",
                "title": f[1] or "Show",
                "subtitle": f[2],
                "collection": True,
                "kind": "album",
                "art": f[0],
                "flags": ["subscribed"],
            } for f in subs[:10]]
            sections.append({"section": {"title": "Subscribed Podcasts", "layout": "tiles"}})
            sections.extend([{"node": n} for n in sub_nodes])

        # Top Charts Shelf
        top = apple_top_charts(state["country"])[:10]
        if top:
            top_nodes = [{
                "id": t["id"],
                "title": t["title"],
                "subtitle": t["author"],
                "collection": True,
                "kind": "album",
                "art": t["feed_url"],
                "flags": ["subscribed"] if is_subscribed(t["feed_url"]) else [],
            } for t in top]
            sections.append({"section": {"title": f"Top Charts ({state['country'].upper()})", "layout": "tiles"}})
            sections.extend([{"node": n} for n in top_nodes])

        return {"entries": sections, "cursor": None}

    # 3. Subscriptions
    if node == "subs":
        subs = get_subscriptions()
        entries = [{
            "node": {
                "id": f"show:{clean_key(f[0])}",
                "title": f[1] or f[0],
                "subtitle": f[2],
                "collection": True,
                "kind": "album",
                "art": f[0],
                "flags": ["subscribed"],
            }
        } for f in subs]
        return {"entries": entries, "cursor": None}

    # 4. Top Charts
    if node == "charts":
        shows = apple_top_charts(state["country"])
        entries = [{
            "node": {
                "id": s["id"],
                "title": s["title"],
                "subtitle": s["author"],
                "collection": True,
                "kind": "album",
                "art": s["feed_url"],
                "flags": ["subscribed"] if is_subscribed(s["feed_url"]) else [],
            }
        } for s in shows]
        return {"entries": entries, "cursor": None}

    # 5. Categories
    if node == "categories":
        entries = [{
            "node": {
                "id": f"genre:{g[0]}:{g[1]}",
                "title": g[1],
                "subtitle": "Category",
                "collection": False,
                "kind": "folder",
            }
        } for g in GENRES]
        return {"entries": entries, "cursor": None}

    if node.startswith("genre:"):
        _, gid, gname = node.split(":", 2)
        shows = apple_search(gname, genre_id=gid)
        entries = [{
            "node": {
                "id": s["id"],
                "title": s["title"],
                "subtitle": s["author"],
                "collection": True,
                "kind": "album",
                "art": s["feed_url"],
                "flags": ["subscribed"] if is_subscribed(s["feed_url"]) else [],
            }
        } for s in shows]
        return {"entries": entries, "cursor": None}

    # 6. Show Node (Returns Episodes)
    # 6. Show Node (Returns Episodes)
    if node.startswith("show:"):
        key = node[len("show:"):]
        # Resolve feed_url from subscriptions or the cached shows table
        feed_url = None
        for f in get_subscriptions():
            if clean_key(f[0]) == key:
                feed_url = f[0]
                break
        if not feed_url:
            cached = get_show_by_key(key)
            if cached:
                feed_url = cached[0]
        if not feed_url:
            raise Failure(-32602, f"Feed for node {node} not cached")

        parsed = parse_feed(feed_url)
        entries = []
        for ep in parsed["episodes"]:
            track = ep["track"]
            ep_metadata_cache[track["key"]] = (ep["audio_url"], ep["chapters"], parsed["artwork"])
            entries.append({"track": track})
        return {"entries": entries, "cursor": None}

    raise Failure(-32602, f"Unknown node: {node}")

def rpc_search(params):
    query = (params.get("query") or "").strip()
    if not query:
        return {"entries": [], "cursor": None}

    # URL search
    if query.startswith(("http://", "https://")):
        try:
            feed = parse_feed(query)
            cache_show(query, feed["title"], feed["author"], feed["artwork"])
            add_subscription(query, feed["title"], feed["author"], feed["artwork"])
            node_id = f"show:{clean_key(query)}"
            return {
                "entries": [{
                    "node": {
                        "id": node_id,
                        "title": feed["title"],
                        "subtitle": feed["author"],
                        "collection": True,
                        "kind": "album",
                        "art": query,
                        "flags": ["subscribed"],
                    }
                }],
                "cursor": None,
            }
        except Exception as e:  # noqa: BLE001 - network feed parse failures
            raise Failure(-32000, f"Cannot parse feed URL: {e}")

    # Text query via Apple API
    shows = apple_search(query)
    entries = [{
        "node": {
            "id": s["id"],
            "title": s["title"],
            "subtitle": s["author"],
            "collection": True,
            "kind": "album",
            "art": s["feed_url"],
            "flags": ["subscribed"] if is_subscribed(s["feed_url"]) else [],
        }
    } for s in shows]
    return {"entries": entries, "cursor": None}

def rpc_sync(params):
    collection = params.get("collection")
    token = params.get("token")
    if not collection.startswith("show:"):
        raise Failure(-32602, f"Invalid collection id: {collection}")

    key = collection[len("show:"):]
    feed_url = None
    for f in get_subscriptions():
        if clean_key(f[0]) == key:
            feed_url = f[0]
            break
    if not feed_url:
        cached = get_show_by_key(key)
        if cached:
            feed_url = cached[0]
    if not feed_url:
        raise Failure(-32602, "Collection not in subscriptions")

    parsed = parse_feed(feed_url)
    tracks = [ep["track"] for ep in parsed["episodes"]]
    new_token = hashlib.sha256("\n".join(t["key"] for t in tracks).encode()).hexdigest()[:16]

    if token == new_token:
        return {"unchanged": True, "tracks": [], "cursor": None, "token": token}

    return {"unchanged": False, "tracks": tracks, "cursor": None, "token": new_token}

def rpc_open(params):
    global stream_counter
    key = params.get("key")
    meta = ep_metadata_cache.get(key)
    if not meta and key.startswith("ep:"):
        # Fallback: the track key embeds the feed key. Resolve and parse.
        feed_key, _, _ = key[3:].partition(":")
        feed_url = None
        for f in get_subscriptions():
            if clean_key(f[0]) == feed_key:
                feed_url = f[0]
                break
        if not feed_url:
            cached = get_show_by_key(feed_key)
            if cached:
                feed_url = cached[0]
        if feed_url:
            parsed = parse_feed(feed_url)
            for ep in parsed["episodes"]:
                t = ep["track"]
                ep_metadata_cache[t["key"]] = (ep["audio_url"], ep["chapters"], parsed["artwork"])
            meta = ep_metadata_cache.get(key)
    if not meta:
        raise Failure(-32000, f"Unknown track key: {key}")

    audio_url, chapters, _ = meta

    # Probe content-length
    length = None
    req = urllib.request.Request(audio_url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            cl = resp.getheader("Content-Length")
            if cl and cl.isdigit():
                length = int(cl)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        pass

    with streams_lock:
        stream_counter += 1
        sid = f"s{stream_counter}"
        streams[sid] = StreamInstance(audio_url, length, chapters)

    return {
        "stream": sid,
        "hint": "mp3" if audio_url.endswith(".mp3") else "m4a",
        "length": length,
        "seekable": True,
        "live": False,
        "buffer": "ahead",
        "chapters": chapters[:500],
    }

def rpc_read(params):
    sid = params.get("stream")
    with streams_lock:
        stream = streams.get(sid)
    if not stream:
        raise Failure(-32000, f"Stream {sid} not open")

    offset = int(params.get("offset") or 0)
    want = min(int(params.get("len") or 0), CHUNK_CAP)
    end = offset + want - 1

    data = stream.read_range(offset, end)
    return {"data": base64.b64encode(data).decode("ascii")}

def rpc_close(params):
    sid = params.get("stream")
    with streams_lock:
        stream = streams.pop(sid, None)
    if stream:
        stream.close()

def rpc_cover(params):
    key = params.get("key")
    # Node cover passes feed_url; Track key looks up cached feed artwork
    url = key if key.startswith("http") else None
    if not url:
        meta = ep_metadata_cache.get(key)
        if meta:
            url = meta[2]
    if not url:
        return None

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read(700 * 1024)
            mime = resp.getheader("Content-Type", "image/jpeg")
            return {"mime": mime, "data": base64.b64encode(data).decode("ascii")}
    except Exception:  # noqa: BLE001 - image fetch failures
        return None

def rpc_link(params):
    item = params.get("item")
    if item.startswith("ep:"):
        meta = ep_metadata_cache.get(item)
        if meta:
            return {"url": meta[0]}
    return None

def rpc_action(params):
    action = params.get("action")
    items = params.get("items") or []

    if action == "subscribe":
        flags = {}
        for item in items:
            feed_url = None
            if item.startswith("show:"):
                # Node key
                for f in get_subscriptions():
                    if f[0] == item:
                        feed_url = f[0]
            if feed_url:
                add_subscription(feed_url)
                flags[item] = ["subscribed"]
        return {"message": "Subscribed to podcast", "flags": flags}

    if action == "unsubscribe":
        flags = {}
        for item in items:
            if item.startswith("show:"):
                k = item[len("show:"):]
                for f in get_subscriptions():
                    if clean_key(f[0]) == k:
                        remove_subscription(f[0])
                        flags[item] = []
        return {"message": "Unsubscribed", "flags": flags}

    if action == "add_feed":
        p = params.get("params") or {}
        url = p.get("url", "").strip()
        if not url:
            raise Failure(-32602, "Feed URL required")
        parsed = parse_feed(url)
        add_subscription(url, parsed["title"], parsed["author"], parsed["artwork"])
        return {"message": f"Added {parsed['title']}"}

    raise Failure(-32602, f"Unknown action {action}")

def rpc_flags(params):
    items = params.get("items") or []
    flags = {}
    subs = {clean_key(f[0]): f[0] for f in get_subscriptions()}
    for it in items:
        if it.startswith("show:"):
            k = it[len("show:"):]
            flags[it] = ["subscribed"] if k in subs else []
    return {"flags": flags}

# ── Dispatcher Loop ────────────────────────────────────────────────

METHODS = {
    "hello": rpc_hello,
    "source.browse": rpc_browse,
    "source.search": rpc_search,
    "source.sync": rpc_sync,
    "source.open": rpc_open,
    "source.read": rpc_read,
    "source.close": rpc_close,
    "source.cover": rpc_cover,
    "source.link": rpc_link,
    "source.action": rpc_action,
    "source.flags": rpc_flags,
}

def handle_request(req_id, method, params):
    handler = METHODS.get(method)
    try:
        if handler is None:
            raise Failure(-32601, f"Method not found: {method}")
        result = handler(params)
        send({"jsonrpc": "2.0", "id": req_id, "result": result})
    except Failure as e:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": e.code, "message": str(e)}})
    except Exception as e:  # noqa: BLE001 - catch-all for plugin handler safety
        log(f"Unhandled error in {method}: {e}")
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}})

def main():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

    for line in sys.stdin.buffer:
        raw = line.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - JSON parse errors on stdin
            log(f"Invalid JSON frame: {e}")
            continue

        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "shutdown":
            send({"jsonrpc": "2.0", "id": rid, "result": None})
            break

        pool.submit(handle_request, rid, method, params)

    pool.shutdown(wait=False, cancel_futures=True)
    fan.shutdown(wait=False, cancel_futures=True)

if __name__ == "__main__":
    main()