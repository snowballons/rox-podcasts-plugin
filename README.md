# Rox Podcast Plugin

A source plugin for the [Rox music player](https://github.com/zealsprince/rox) that
brings podcasts into the library: browse, search, subscribe, sync, and stream
episodes from Apple's podcast directory and any publisher RSS feed.

## Features

- **Discover** shows through Apple's podcast charts and category browse
- **Search** shows by name, or paste an RSS feed URL to preview a show
- **Subscribe** to a show and keep its episodes in the library
- **Sync** episodes on demand and when the plugin starts
- **Stream** audio with HTTP range requests, seeking, and Podlove chapters
- **Add by URL** action for pasting feed URLs directly from the panel menu
- **Show covers** from the feed's artwork or Apple's directory metadata

## Requirements

- Rox Player with plugins enabled (Settings > Plugins)
- Python 3 on the host machine (standard library only — no pip dependencies)

## Installation

1. Turn on **Enable Plugins** in Settings > Plugins
2. Press **Reveal Folder** to open the plugins directory
3. Drop this folder in (it must be named `podcast` to match its `plugin.json` id)
4. Switch the plugin on and approve the trust card

## Configuration

Two settings on the Plugins page:

| Setting | Description | Default |
|---------|-------------|---------|
| Top Charts Country | Two-letter ISO country code for charts (e.g. `us`, `gb`, `de`) | `us` |
| Max Episodes per Feed | Cap parsing at the newest N episodes per feed | `300` |

## Architecture

The plugin is a single Python process that speaks newline-delimited JSON-RPC 2.0
over stdin/stdout with the Rox host. It uses only the Python standard library.

```
Rox Host (Rust)
    │  JSON-RPC 2.0 over stdin/stdout
    ▼
podcast.py
    ├── Apple Directory Gateway   (itunes.apple.com search / charts / lookup)
    ├── RSS Parser Engine        (ElementTree, Podlove psc:chapters)
    ├── Streaming Proxy          (HTTP range, keep-alive, 3xx follow)
    ├── SQLite persistence       (subscriptions, show cache — in data_dir, hash-safe)
    └── JSON-RPC handlers        (hello, browse, search, sync, open, read, close,
                                   cover, link, action, flags, shutdown)
```

### Key design decisions

- **Opaque keys**: `sha256(feed_url)[:24]` for shows, `ep:<feedhash>:<guidhash>` for
  episodes. Keys never change across sessions or plugin versions, so library rows
  are never orphaned.
- **Sync token**: `sha256(all track keys)[:16]` — the same pattern as Rox's own
  `internet-archive` example. Lets the host skip a sync that would write nothing.
- **Storage in `data_dir`**: The plugin never writes into its own folder (that
  would change its SHA-256 hash and switch itself off). All state lives in the
  `data_dir` handed to it in `hello`.
- **Thread pool**: 8 workers serve browse/search/sync/open/read concurrently so
  a slow feed fetch never blocks a running stream.

## Development

```bash
# Sync to the live plugin folder
./build.sh

# Test the plugin directly over stdio (no Rox needed)
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"hello","params":{"api":1,"config":{},"data_dir":"/tmp/podcast-data","platform":"linux-x86_64","features":["home","tiles","chapters"]}}' \
  '{"jsonrpc":"2.0","id":2,"method":"source.browse","params":{"node":null,"cursor":null}}' \
  | python3 -B -u podcast.py

# Lint
ruff check podcast.py
```

## License

AGPL-3.0-only — same as the Rox plugin API contract.