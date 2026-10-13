"""JSON-RPC method handlers (source.*): the plugin's visible behavior.

Wires together protocol, store, directory, feed and stream. This is the
only module that imports from siblings other than protocol.
"""

import base64
import hashlib
import urllib.error
import urllib.request

from directory import GENRES, apple_search, apple_top_charts
from feed import parse_feed
from protocol import (
    API,
    CHUNK_CAP,
    HTTP_TIMEOUT,
    USER_AGENT,
    VERSION,
    Failure,
    clean_key,
    fan,
    has_feature,
    hint_for_url,
    log,
    make_node,
    make_section,
    mime_of,
    send,
    show_art_key,
    state,
)
from store import (
    add_subscription,
    cache_show,
    get_show,
    get_show_artwork,
    get_subscriptions,
    init_db,
    is_subscribed,
    remove_subscription,
    resolve_feed_url,
)
from stream import StreamInstance, ep_metadata_cache, stream_ids, streams, streams_lock


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
            make_node(
                "home",
                "Home",
                "Top charts & subscriptions",
                False,
                "folder",
                "",
                None,
                True,
            ),
            make_node("subs", "Subscriptions", "Your saved shows", False, "folder"),
            make_node(
                "charts",
                "Top Charts",
                f"Top shows in {state['country'].upper()}",
                False,
                "folder",
            ),
            make_node("categories", "Categories", "Browse by genre", False, "folder"),
        ]
        return {"entries": entries, "cursor": None}

    # 2. Home view
    if node == "home":
        sections = []
        # Charts fetch runs alongside the local subscription read so a
        # slow directory never blocks what is already on disk. Bounded
        # below the browse timeout; a miss just drops the shelf.
        pending_top = fan.submit(apple_top_charts, state["country"])
        subs = get_subscriptions()
        if subs:
            sub_nodes = [
                make_node(
                    f"show:{clean_key(f[0])}",
                    f[1] or "Show",
                    f[2],
                    True,
                    "album",
                    show_art_key(f[0]),
                    ["subscribed"],
                )
                for f in subs[:10]
            ]
            sections.extend(make_section("Subscribed Podcasts", sub_nodes, tiles=True))

        # Top Charts Shelf
        try:
            top = pending_top.result(timeout=12)[:10]
        except Exception as e:  # noqa: BLE001 - slow/failed charts drop the shelf
            log(f"home charts missed: {e}")
            top = []
        if top:
            top_nodes = [
                make_node(
                    t["id"],
                    t["title"],
                    t["author"],
                    True,
                    "album",
                    show_art_key(t["feed_url"]),
                    ["subscribed"] if is_subscribed(t["feed_url"]) else [],
                )
                for t in top
            ]
            sections.extend(
                make_section(
                    f"Top Charts ({state['country'].upper()})", top_nodes, tiles=True
                )
            )

        return {"entries": sections, "cursor": None}

    # 3. Subscriptions
    if node == "subs":
        subs = get_subscriptions()
        entries = [
            make_node(
                f"show:{clean_key(f[0])}",
                f[1] or f[0],
                f[2],
                True,
                "album",
                show_art_key(f[0]),
                ["subscribed"],
            )
            for f in subs
        ]
        return {"entries": entries, "cursor": None}

    # 4. Top Charts
    if node == "charts":
        shows = apple_top_charts(state["country"])
        entries = [
            make_node(
                s["id"],
                s["title"],
                s["author"],
                True,
                "album",
                show_art_key(s["feed_url"]),
                ["subscribed"] if is_subscribed(s["feed_url"]) else [],
            )
            for s in shows
        ]
        return {"entries": entries, "cursor": None}

    # 5. Categories
    if node == "categories":
        entries = [
            make_node(f"genre:{g[0]}:{g[1]}", g[1], "Category", False, "folder")
            for g in GENRES
        ]
        return {"entries": entries, "cursor": None}

    if node.startswith("genre:"):
        _, gid, gname = node.split(":", 2)
        shows = apple_search(gname, genre_id=gid)
        entries = [
            make_node(
                s["id"],
                s["title"],
                s["author"],
                True,
                "album",
                show_art_key(s["feed_url"]),
                ["subscribed"] if is_subscribed(s["feed_url"]) else [],
            )
            for s in shows
        ]
        return {"entries": entries, "cursor": None}

    # 6. Show Node (Returns Episodes)
    if node.startswith("show:"):
        key = node[len("show:") :]
        feed_url = resolve_feed_url(key)
        if not feed_url:
            raise Failure(-32602, f"Feed for node {node} not cached")

        parsed = parse_feed(feed_url)
        entries = []
        for ep in parsed["episodes"]:
            track = ep["track"]
            ep_metadata_cache[track["key"]] = (
                ep["audio_url"],
                ep["chapters"],
                parsed["artwork"],
                ep.get("length"),
            )
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
            # Do NOT auto-subscribe; preview only
            node_id = f"show:{clean_key(query)}"
            return {
                "entries": [
                    make_node(
                        node_id,
                        feed["title"],
                        feed["author"],
                        True,
                        "album",
                        show_art_key(query),
                        ["subscribed"] if is_subscribed(query) else [],
                    )
                ],
                "cursor": None,
            }
        except Exception as e:  # noqa: BLE001 - network feed parse failures
            raise Failure(-32000, f"Cannot parse feed URL: {e}")

    # Text query via Apple API
    shows = apple_search(query)
    entries = [
        make_node(
            s["id"],
            s["title"],
            s["author"],
            True,
            "album",
            show_art_key(s["feed_url"]),
            ["subscribed"] if is_subscribed(s["feed_url"]) else [],
        )
        for s in shows
    ]
    return {"entries": entries, "cursor": None}


def rpc_sync(params):
    collection = params.get("collection")
    token = params.get("token")
    if not collection.startswith("show:"):
        raise Failure(-32602, f"Invalid collection id: {collection}")

    key = collection[len("show:") :]
    feed_url = resolve_feed_url(key)
    if not feed_url:
        raise Failure(-32602, "Collection not in subscriptions")

    parsed = parse_feed(feed_url)
    tracks = [ep["track"] for ep in parsed["episodes"]]
    new_token = hashlib.sha256(
        "\n".join(t["key"] for t in tracks).encode()
    ).hexdigest()[:16]

    if token == new_token:
        return {"unchanged": True, "tracks": [], "cursor": None, "token": token}

    return {"unchanged": False, "tracks": tracks, "cursor": None, "token": new_token}


def rpc_open(params):
    key = params.get("key")
    meta = ep_metadata_cache.get(key)
    if not meta and key.startswith("ep:"):
        # Fallback: the track key embeds the feed key. Resolve and parse.
        feed_key, _, _ = key[3:].partition(":")
        feed_url = resolve_feed_url(feed_key)
        if feed_url:
            parsed = parse_feed(feed_url)
            for ep in parsed["episodes"]:
                t = ep["track"]
                ep_metadata_cache[t["key"]] = (
                    ep["audio_url"],
                    ep["chapters"],
                    parsed["artwork"],
                    ep.get("length"),
                )
            meta = ep_metadata_cache.get(key)
    if not meta:
        raise Failure(-32000, f"Unknown track key: {key}")

    audio_url, chapters, _, cached_length = meta

    # Prefer the RSS enclosure length; a HEAD probe re-walks the whole
    # redirect chain, so it is only a fallback for feeds without one.
    length = cached_length
    if length is None:
        req = urllib.request.Request(
            audio_url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                cl = resp.getheader("Content-Length")
                if cl and cl.isdigit():
                    length = int(cl)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            pass

    with streams_lock:
        sid = f"s{next(stream_ids)}"
        streams[sid] = StreamInstance(audio_url, length, chapters)

    result = {
        "stream": sid,
        "hint": hint_for_url(audio_url),
        "length": length,
        "seekable": length is not None,
        "live": False,
        "buffer": "ahead",
    }
    if chapters and has_feature("chapters"):
        result["chapters"] = chapters[:500]
    return result


def rpc_read(params):
    sid = params.get("stream")
    with streams_lock:
        stream = streams.get(sid)
    if not stream:
        raise Failure(-32000, f"Stream {sid} not open")

    offset = int(params.get("offset") or 0)
    want = min(int(params.get("len") or 0), CHUNK_CAP)
    if want <= 0:
        return {"data": ""}
    if stream.length is not None and offset >= stream.length:
        return {"data": ""}
    end = offset + want - 1
    if stream.length is not None:
        end = min(end, stream.length - 1)

    data = stream.read_range(offset, end, want)
    return {"data": base64.b64encode(data).decode("ascii")}


def rpc_close(params):
    sid = params.get("stream")
    with streams_lock:
        stream = streams.pop(sid, None)
    if stream:
        stream.close()


def rpc_cover(params):
    key = params.get("key")
    url = None

    if key.startswith("show:"):
        # Show node cover: resolve show hash → feed_url → artwork.
        # Art keys are unified as show:<hash>, but also accept a raw
        # artwork URL for backwards compatibility with older caches.
        feed_key = key[len("show:") :]
        feed_url = resolve_feed_url(feed_key)
        if feed_url:
            url = get_show_artwork(feed_url)
    elif key.startswith("ep:"):
        # Episode cover: look up in metadata cache
        meta = ep_metadata_cache.get(key)
        if meta:
            url = meta[2]
    elif key.startswith("http"):
        # Direct URL (fallback)
        url = key

    if not url:
        return None

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read(700 * 1024 + 1)
            if len(data) > 700 * 1024:
                return None
            mime = mime_of(data)
            if not mime:
                return None
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
            if not item.startswith("show:"):
                continue
            k = item[len("show:") :]
            feed_url = resolve_feed_url(k)
            if not feed_url:
                continue
            # Preserve known metadata so covers/flags keep working.
            title, author, artwork, genre = "", "", "", ""
            for f in get_subscriptions():
                if clean_key(f[0]) == k:
                    _, title, author, artwork, genre = f
                    break
            if not title:
                cached = get_show(feed_url)
                if cached:
                    title, author, artwork, genre = cached
            if not title:
                try:
                    parsed = parse_feed(feed_url)
                    title, author, artwork = (
                        parsed["title"],
                        parsed["author"],
                        parsed["artwork"],
                    )
                    cache_show(feed_url, title, author, artwork)
                except Failure:
                    pass
            add_subscription(feed_url, title, author, artwork, genre)
            flags[item] = ["subscribed"]
        return {"message": "Subscribed to podcast", "flags": flags}

    if action == "unsubscribe":
        flags = {}
        for item in items:
            if item.startswith("show:"):
                k = item[len("show:") :]
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
        cache_show(url, parsed["title"], parsed["author"], parsed["artwork"])
        add_subscription(url, parsed["title"], parsed["author"], parsed["artwork"])
        return {"message": f"Added {parsed['title']}"}

    raise Failure(-32602, f"Unknown action {action}")


def rpc_flags(params):
    items = params.get("items") or []
    flags = {}
    subs = {clean_key(f[0]): f[0] for f in get_subscriptions()}
    for it in items:
        if it.startswith("show:"):
            k = it[len("show:") :]
            flags[it] = ["subscribed"] if k in subs else []
    return {"flags": flags}


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
        send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": e.code, "message": str(e)},
            }
        )
    except Exception as e:  # noqa: BLE001 - catch-all for plugin handler safety
        log(f"Unhandled error in {method}: {e}")
        send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)},
            }
        )
