"""HTTP range streaming: kept-alive connections serving source.read.

Owns the open-stream registry and the episode metadata cache that open
populates and read/cover/link consume. Stream ids come from an iterator
(archive.py pattern) so no cross-module global statement is needed.
Depends only on protocol.
"""

import http.client
import threading
import urllib.error
import urllib.parse

from protocol import HTTP_TIMEOUT, USER_AGENT, Failure, log

# Podcast prefix chains (podtrac/pdst/vpixl/...) routinely need 5 hops
# before bytes; 10 matches urllib/curl defaults and our feed policy.
MAX_REDIRECTS = 10


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

    def read_range(self, start, end, want):
        with self.lock:
            hops = []
            seen = {self.resolved_url}
            for _ in range(MAX_REDIRECTS + 1):
                parts = urllib.parse.urlsplit(self.resolved_url)
                origin = (parts.scheme, parts.hostname, parts.port)
                if self.conn is None or origin != self.origin_tuple:
                    if self.conn:
                        try:
                            self.conn.close()
                        except OSError as e:
                            log(f"stream close error: {e}")
                    cls = (
                        http.client.HTTPSConnection
                        if parts.scheme == "https"
                        else http.client.HTTPConnection
                    )
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
                        if not loc:
                            raise Failure(-32000, "Redirect without Location")
                        nxt = urllib.parse.urljoin(self.resolved_url, loc)
                        hops.append(f"{resp.status} {self.resolved_url} -> {nxt}")
                        if nxt in seen:
                            log(
                                f"redirect loop detected: {nxt} (hops: {'; '.join(hops)})"
                            )
                            raise Failure(-32000, f"Redirect loop detected: {nxt}")
                        seen.add(nxt)
                        self.resolved_url = nxt
                        continue

                    if resp.status == 416:
                        resp.read()
                        return b""
                    if resp.status not in (200, 206):
                        raise Failure(-32000, f"Upstream HTTP error: {resp.status}")

                    data = resp.read()
                    if resp.status == 200:
                        # Server ignored Range and sent the whole file:
                        # slice the requested window instead of the full body.
                        if start >= len(data):
                            return b""
                        return data[start : start + want]
                    return data[:want]
                except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                    if self.conn:
                        try:
                            self.conn.close()
                        except OSError as e2:
                            log(f"stream close error: {e2}")
                        self.conn = None
                    raise Failure(-32000, f"Stream read error: {e}")

            log(
                f"too many redirects ({len(hops)} hops from {self.origin_url}, "
                f"last: {self.resolved_url})"
            )
            raise Failure(-32000, f"Too many redirects ({len(hops)} hops)")


streams = {}
streams_lock = threading.Lock()
stream_ids = iter(range(1, 1 << 62))

# Ep cache: map track key -> (audio_url, chapters, artwork, length)
ep_metadata_cache = {}
