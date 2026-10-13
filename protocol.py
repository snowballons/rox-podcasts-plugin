"""Shared protocol plumbing for the Rox podcast plugin.

Owns every process-wide global (config state, locks, the fan pool) so the
other modules import from exactly one place and never from each other
(except handlers, which wires them together). Dependency leaf: imports
nothing local.
"""

import concurrent.futures
import email.utils
import hashlib
import json
import sys
import threading

API = 1
VERSION = "1.0.0"
USER_AGENT = f"RoxPodcastPlugin/{VERSION} (+https://github.com/zealsprince/rox)"
HTTP_TIMEOUT = 12
CHUNK_CAP = 256 * 1024
PAGE_SIZE = 30
STRING_CAP = 4000

# Thread safety & global state
out_lock = threading.Lock()
state = {
    "data_dir": None,
    "country": "us",
    "max_episodes": 300,
    "features": set(),
}

fan = concurrent.futures.ThreadPoolExecutor(max_workers=8)


def send(frame):
    payload = (
        json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
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


def clip(text):
    return str(text or "").strip()[:STRING_CAP]


def has_feature(name):
    return name in state["features"]


def make_node(
    nid, title, subtitle="", collection=False, kind="", art="", flags=None, home=False
):
    """Build a browse node, gating optional fields behind hello features."""
    body = {
        "id": nid,
        "title": clip(title),
        "subtitle": clip(subtitle),
        "collection": collection,
    }
    if kind and has_feature("node-kind"):
        body["kind"] = kind
    if art and has_feature("node-art"):
        body["art"] = art
    if flags is not None and has_feature("flags"):
        body["flags"] = flags
    if home and has_feature("home"):
        body["home"] = True
    return {"node": body}


def make_section(title, entries, tiles=False):
    """Wrap entries under a heading; plain rows when sections unsupported."""
    if not entries or not has_feature("sections"):
        return entries
    heading = {"title": clip(title)}
    if tiles and has_feature("tiles"):
        heading["layout"] = "tiles"
    return [{"section": heading}] + entries


def clean_key(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def show_art_key(feed_url):
    """Single art scheme for every show node: the node id itself."""
    return f"show:{clean_key(feed_url)}"


def mime_of(data):
    """Detect mime type from magic bytes."""
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def hint_for_url(audio_url):
    lower = audio_url.split("?", 1)[0].lower()
    if lower.endswith(".mp3"):
        return "mp3"
    if lower.endswith((".m4a", ".mp4", ".m4b", ".aac")):
        return "m4a"
    if lower.endswith((".ogg", ".oga", ".opus")):
        return "ogg"
    if lower.endswith(".wav"):
        return "wav"
    return ""


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
            return (
                int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
            ) * 1000
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
