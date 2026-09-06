# txt-to-epub

> Multi-source web novel fetcher & EPUB converter with embedded font support.

[中文版](README_zh.md)

Fetch from lightnovel.app, syosetu.org, wenku8.net, novelia.cc and output font-embedded EPUBs compatible with Kindle, Kobo, Apple Books, and more.

## Features

- **Multiple fetch sources** — lightnovel.app (pure-Python SignalR LongPolling), novelia.cc web + 文库版, lightnovel.fun/轻之国度 (HTTP+login), esjzone & 真白萌 (CDP+login), wenku8.net (pure HTTP, GBK), syosetu.org (CDP)
- **Font-embedded EPUB** — auto-download WOFF2 fonts, convert to TTF, embed in EPUB; solves lightnovel.app font obfuscation. The obfuscation font differs **per chapter** (per-fetch randomized), so each chapter keeps its own embedded font.
- **Illustration embedding** — auto-download chapter images, rewrite `src` to local paths, embed in EPUB
- **Fetch memory** — per-chapter tracking; re-runs skip already-fetched chapters (great for ongoing serials)
- **`--all` auto-pack** — fetch all chapters then automatically pack into EPUB
- **`--pack-only`** — skip fetching, pack existing HTML files directly into EPUB
- **Encoding detection + chapter parsing** — smart TXT encoding detection, chapter title recognition, paragraph merging, noise removal
- **Pure-Python LightNovelShelf client** — SignalR LongPolling, no Dart SDK / WebSocket TLS workaround

## Quick Start

```bash
# 1. Extract txt-to-epub-dist.zip
# 2. One-click dependency install
setup.bat

# 3. Edit config.json, add your lightnovel.app refresh_token (lightnovel source only)
#    Fetch it automatically: python ebook.py refresh-token  (reads browser IndexedDB)

# 4. Start using
python ebook.py lightnovel --bid <BID> --all                       # fetch + auto-pack EPUB
python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"
python ebook.py syosetu -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>
python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "Title" --author "Author"
```

## Supported Sources

| Source | Method | Font EPUB | Illustrations | Requires |
|--------|--------|:---------:|:-------------:|----------|
| [lightnovel.app](https://www.lightnovel.app) | SignalR LongPolling | ✅ | ✅ | refresh_token |
| [novelia.cc](https://novelia.cc) | REST API | — | — | — |
| novelia 文库版 | REST API | ✅ | ✅ | — |
| [lightnovel.fun](https://www.lightnovel.fun) 轻之国度 | HTTP (curl_cffi) + login | — | — | lk.username/password |
| [esjzone.one](https://www.esjzone.one) | CDP (Edge browser) + login | — | — | esj.username/password + Edge |
| [masiro.me](https://masiro.me) 真白萌 | CDP (Edge browser) + login | — | — | masiro.username/password + Edge |
| [wenku8.net](https://www.wenku8.net) | HTTP (GBK HTML) | — | ✅ | — |
| [ixinzhi 轻小说文库库](https://github.com/ixinzhi) | GitHub direct EPUB download | finished | varies | — |
| [syosetu.org](https://syosetu.org) | CDP (Edge browser) | — | ✅ | Edge browser |
| Local TXT | stdlib | — | — | — |

## Installation

### Prerequisites

- Python 3.9+
- Microsoft Edge (for syosetu/esjzone/masiro CDP fetchers; auto-detected)

### Setup

```bash
# Automatic (Windows)
setup.bat

# Manual
pip install -r requirements.txt
```

The LightNovelShelf client is **pure Python** (SignalR LongPolling via curl_cffi). No Dart SDK / bridge executable needed.

## Configuration

Copy `config.example.json` to `config.json` and fill in your settings:

```json
{
  "lightnovel": {
    "refresh_token": "YOUR_REFRESH_TOKEN_HERE"
  },
  "fetch_dir": "./fetch",
  "edge_path": null
}
```

Or use environment variables: `LIGHTNOVEL_REFRESH_TOKEN`, `FETCH_DIR`, `EDGE_PATH`.

## Usage

### Unified CLI (`ebook.py`)

```bash
# lightnovel.app
python ebook.py lightnovel --bid <BID> --chapter <CID> --html     # single chapter HTML
python ebook.py lightnovel --bid <BID> --all                       # all chapters → auto EPUB
python ebook.py lightnovel --bid <BID> --all --no-epub             # fetch only, skip packing
python ebook.py lightnovel --bid <BID> --download                  # whole-book EPUB directly (needs coins/permission)
python ebook.py lightnovel --bid <BID> --chapter 1 --convert t2s   # server-side simplified conversion
python ebook.py lightnovel --bid <BID> --pack-only                 # pack existing files
python ebook.py lightnovel --bid <BID> --pack-only --author "Author"

# syosetu.org
python ebook.py syosetu -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>

# wenku8.net
python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"

# 轻小说文库 ready-made EPUBs (ixinzhi GitHub direct links, no scraping)
python ebook.py ixzbhi "86-不存在的战区"            # search + download best match
python ebook.py ixzbhi "书名" --list                 # list matches only
python ebook.py ixzbhi "书名" --offline              # use cached index

# novelia.cc
python ebook.py novelia "https://n.novelia.cc/novel/<SOURCE>/<ID>" -o <OUTPUT>.txt

# novelia bunko edition (published volumes, one dir per book)
python ebook.py wenku "https://n.novelia.cc/wenku/<WID>" --list
python ebook.py wenku "https://n.novelia.cc/wenku/<WID>"

# lightnovel.fun (轻之国度) — requires lk.username/password in config.json
python ebook.py lk "https://www.lightnovel.fun/<LKID>"

# esjzone (requires esj.username/password in config.json; CDP-rendered)
python ebook.py esj "https://www.esjzone.one/forum/<BOARD>/<ESJID>/"

# masiro 真白萌 (requires masiro.username/password; CDP Cloudflare bypass + login)
python ebook.py masiro "https://masiro.me/admin/novelView?novel_id=<MSID>"

# Auto-fetch lightnovel.app RefreshToken from browser IndexedDB (browser-free disk read)
python ebook.py refresh-token

# masiro.me (真白萌) — requires masiro.username/password in config.json
python ebook.py masiro "https://masiro.me/admin/novelView?novel_id=<MSID>"

# TXT → EPUB
python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "Title" --author "Author"

# Already-typeset TXT → EPUB (layout-preserving: one paragraph per line / blank line =
# scene break, no merge or noise-clean; optional OpenCC simplified/traditional)
python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --preserve-layout \
    --convert t2s --cover cover.jpg --images-dir <IMAGE_DIR>
# --convert t2s|s2t local OpenCC; --chapter-regex R custom chapter-title regex

# HTML dir → font-embedded EPUB
python ebook.py pack <BOOK_DIR> --author "Author"

# Manage fetch memory
python ebook.py memory list              # list all records
python ebook.py memory show lightnovel   # show details
python ebook.py memory forget <BID>      # clear one book's memory
```

### Common Options

| Flag | Description |
|------|-------------|
| `--all` | Fetch all chapters; auto-pack EPUB by default |
| `--force` | Ignore memory, force re-fetch |
| `--pack-only` | Skip fetch, pack existing files into EPUB |
| `--no-epub` | Fetch but don't auto-pack |
| `--author NAME` | Set author metadata in EPUB |
| `-o, --output PATH` | Specify EPUB output path |

### Direct Script Usage

Each script under `scripts/` can be invoked independently:

```bash
python scripts/convert.py <INPUT>.txt -o <OUTPUT>.epub --title "Title"
python scripts/lightnovel_api.py --bid <BID> --chapter <CID> --html
python scripts/syosetu_fetch.py -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>
python scripts/wenku8_fetch.py -u "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"
python scripts/lightnovel_decode.py --html-file page.html --font-url "https://..."
```

## How It Works

```
┌─ lightnovel.app ───────────────────────────────────────────────┐
│  Dart SignalR bridge (.exe)                                    │
│    → HTTP token exchange → WebSocket MessagePack → gzip body   │
│    → WOFF2 font download (curl_cffi TLS impersonation)         │
│    → PUA codepoint removal (fontTools)                         │
│    → Self-contained HTML with base64-embedded font             │
│    → Image extraction & download                               │
└──────────────────────┬─────────────────────────────────────────┘
                       │  html2epub_font.py
                       │  WOFF2 → TTF (fontTools)
                       │  XHTML + CSS + EPUB3 packaging
                       ▼
                 ┌──────────┐
                 │  .epub   │  ◄── font-embedded + illustrations
                 └──────────┘      Kindle / Kobo / Apple Books
```

### Why SignalR LongPolling (not WebSocket)?

lightnovel.app's SignalR server performs **WebSocket TLS fingerprinting** — Python WebSocket libraries (`websockets`, `websocket-client`) are rejected on the handshake. The SignalR **LongPolling** transport is plain HTTP(S), which curl_cffi (impersonating a browser TLS fingerprint) passes. So chapter content is fetched over LongPolling with no WebSocket and no language runtime beyond Python. Protocol details (negotiate → handshake → invoke → poll) live in `lightnovel_client.py`.

### Why Font Embedding?

lightnovel.app delivers chapter text with characters mapped to **Unicode PUA** (U+E000–F8FF). The WOFF2 font contains the correct glyphs. By embedding the font as TTF in the EPUB, any reader can display the correct text without needing the original font installed.

## Kindle Compatibility

- Sideload the EPUB and enable **"Publisher Font"** in the Aa menu
- For best results, convert to KFX via Calibre + KFX Output plugin

## Script Reference

| Script | Purpose |
|--------|---------|
| `ebook.py` | Unified CLI entry point |
| `scripts/convert.py` | TXT → EPUB (encoding detection, chapter parsing, noise removal) |
| `scripts/pack_preserve.py` | Typeset TXT → EPUB (layout-preserving; optional OpenCC, cover, illustrations) |
| `scripts/lightnovel_api.py` | lightnovel.app API fetcher (pure-Python LongPolling; +download/convert) |
| `scripts/lightnovel_client.py` | lightnovel.app SignalR LongPolling client (msgpack + REST download) |
| `scripts/syosetu_fetch.py` | syosetu.org CDP fetcher (Cloudflare bypass) |
| `scripts/wenku8_fetch.py` | wenku8.net CDP fetcher |
| `scripts/ixzbhi_fetch.py` | 轻小说文库 ready-made EPUB direct download (ixinzhi GitHub; cached index + fuzzy search) |
| `scripts/web_fetch.py` | novelia.cc REST API fetcher |
| `scripts/html2epub_font.py` | HTML directory → font-embedded EPUB (WOFF2→TTF) |
| `scripts/lightnovel_decode.py` | Offline font decoder for snapshots |
| `scripts/fetch_memory.py` | Fetch memory persistence |
| `scripts/html2txt.py` | [experimental] VLM OCR pipeline |
| `scripts/build_decode_map.py` | [experimental] VLM OCR glyph mapping |

## Dependencies

```
requests  websocket-client  fonttools  brotli  curl_cffi  Pillow  opencc-python-reimplemented
```

All installable via `pip install -r requirements.txt`.  
No Dart SDK needed — the LightNovelShelf client is pure Python.

## Notes

- **Config security**: `config.json` contains your lightnovel.app refresh token. It is excluded from the distribution zip and git-ignored.
- **Memory durability**: Fetch memory is stored in `fetch_memory.json`. This file is personal and excluded from distribution.
- **Experimental OCR**: The VLM OCR font decoding path (`build_decode_map.py`, `html2txt.py`) is retained as a draft. It works but has accuracy issues with visually similar characters.
- **Cross-platform**: Fully Python; no language runtime beyond the stock interpreter.

## GitHub Actions remote download

`.github/workflows/download.yml` fetches books on a GitHub cloud runner and produces EPUBs — no local machine needed.

**Usage:** repo → **Actions** → **txt-to-epub download** → **Run workflow** → fill:

| Input | Description |
|---|---|
| `source` | `lightnovel` / `wenku` / `novelia` / `lk` |
| `book_id` | lightnovel=number bid; wenku=bunko URL or WID; novelia=web URL; lk=URL or id |
| `mode` | lightnovel: `all` (per-chapter, no coins) / `download` (ready EPUB, needs coins) / `chapter` |
| `chapter` | SortNum when `mode=chapter` |
| `convert` | lightnovel server-side `t2s` / `s2t` / empty |
| `translation` | novelia source `sakura`/`jp`/`youdao`/`gpt` |

**Secrets (repo → Settings → Secrets and variables → Actions):**
- `LIGHTNOVEL_REFRESH_TOKEN` — required for lightnovel (get locally via `python ebook.py refresh-token`)
- `LK_USERNAME` / `LK_PASSWORD` — only for lk

**Artifact:** download `txt-to-epub-output` (`output.tar.gz`) after the run; with `FETCH_DIR=download` the generated `.epub` files land inside it.

## License

MIT
