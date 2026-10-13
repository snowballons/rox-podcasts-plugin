#!/usr/bin/env python3
"""Rox Player Podcast Plugin (API 1).

Entry point: Rox starts this file as a subprocess and speaks
newline-delimited JSON-RPC 2.0 over stdin/stdout. All behavior lives in
the sibling modules; this file only owns the transport loop.

Implements Apple Podcasts directory discovery, publisher RSS parsing,
HTTP range streaming, native chapters, and library synchronization.
"""

import concurrent.futures
import json
import sys

from handlers import handle_request
from protocol import fan, log, send


def main():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

    # Ends when stdin closes, whether rox shut down cleanly or not.
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
