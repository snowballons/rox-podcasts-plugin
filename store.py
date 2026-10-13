"""SQLite persistence for the Rox podcast plugin.

Lives under hello's data_dir (never the plugin folder, whose hash Rox
approves). Depends only on protocol.
"""

import os
import sqlite3
import threading
import time

from protocol import clean_key

db_conn = None
db_lock = threading.Lock()


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
        row = db_conn.execute(
            "SELECT 1 FROM subscriptions WHERE feed_url = ?", (feed_url,)
        ).fetchone()
        return row is not None


def add_subscription(feed_url, title="", author="", artwork="", genre=""):
    with db_lock:
        db_conn.execute(
            """
            INSERT OR REPLACE INTO subscriptions (feed_url, title, author, artwork, genre, subscribed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (feed_url, title, author, artwork, genre, int(time.time())),
        )
        db_conn.commit()


def remove_subscription(feed_url):
    with db_lock:
        db_conn.execute("DELETE FROM subscriptions WHERE feed_url = ?", (feed_url,))
        db_conn.commit()


def get_subscriptions():
    with db_lock:
        cur = db_conn.execute(
            "SELECT feed_url, title, author, artwork, genre FROM subscriptions ORDER BY subscribed_at DESC"
        )
        return cur.fetchall()


def cache_show(feed_url, title="", author="", artwork="", genre="", episode_count=0):
    """Cache show metadata so browse/open can resolve feed URLs without a live subscription."""
    with db_lock:
        db_conn.execute(
            """
            INSERT OR REPLACE INTO shows (feed_url, title, author, artwork, genre, episode_count, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (feed_url, title, author, artwork, genre, episode_count, int(time.time())),
        )
        db_conn.commit()


def get_show(feed_url):
    """Return cached show metadata (title, author, artwork, genre) or None."""
    with db_lock:
        row = db_conn.execute(
            "SELECT title, author, artwork, genre FROM shows WHERE feed_url = ?",
            (feed_url,),
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


def get_show_artwork(feed_url):
    """Return the cached artwork URL for a show, or None."""
    with db_lock:
        row = db_conn.execute(
            "SELECT artwork FROM shows WHERE feed_url = ?", (feed_url,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        row = db_conn.execute(
            "SELECT artwork FROM subscriptions WHERE feed_url = ?", (feed_url,)
        ).fetchone()
        return row[0] if row and row[0] else None


def resolve_feed_url(key):
    """Resolve a show:<hash> key to its feed_url via subscriptions then shows cache."""
    for f in get_subscriptions():
        if clean_key(f[0]) == key:
            return f[0]
    cached = get_show_by_key(key)
    if cached:
        return cached[0]
    return None
