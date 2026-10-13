"""Publisher RSS feed parser: feed URL in, show metadata + episodes out.

Depends only on protocol (HTTP settings, Failure, clipping, key and
duration helpers, global config state).
"""

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from protocol import (
    HTTP_TIMEOUT,
    USER_AGENT,
    Failure,
    clean_key,
    clip,
    parse_duration_ms,
    parse_pub_date,
    state,
)


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
    show_author = (
        channel.findtext("itunes:author", namespaces=ns)
        or channel.findtext("author")
        or ""
    ).strip()
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

        # The RSS enclosure length saves a HEAD round trip per open
        # (which otherwise re-walks the whole redirect chain).
        enc_length = None
        raw_length = (enclosure.attrib.get("length") or "").strip()
        if raw_length.isdigit() and int(raw_length) > 0:
            enc_length = int(raw_length)

        # Audio mime or extension check
        if not (
            enc_type.startswith("audio/")
            or audio_url.endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus"))
        ):
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
            "title": clip(title),
            "artist": clip(show_author or show_title),
            "album_artist": clip(show_author or show_title),
            "album": clip(show_title),
            "genre": "Podcast",
            "year": pub_year,
            "disc_no": 1,
            "track_no": idx + 1,
            "duration_ms": duration_ms,
            "codec": "MP3"
            if "mpeg" in enc_type or audio_url.endswith(".mp3")
            else "AAC",
            "bitrate_kbps": 128,
            "live": False,
        }

        episodes.append(
            {
                "track": track_entry,
                "audio_url": audio_url,
                "chapters": chapters,
                "length": enc_length,
            }
        )

    return {
        "title": show_title,
        "author": show_author,
        "artwork": show_image,
        "episodes": episodes,
    }
