# txt-to-epub

> Multi-source web novel fetcher & EPUB converter with embedded font support.

[中文版](README_zh.md)

Fetch from lightnovel.app, syosetu.org, wenku8.net, novelia.cc and output font-embedded EPUBs compatible with Kindle, Kobo, Apple Books, and more.

## Features

- **4 fetch sources** — lightnovel.app (Dart SignalR bridge), syosetu.org (CDP Cloudflare bypass), wenku8.net (CDP), novelia.cc (REST API)
- **Font-embedded EPUB** — auto-download WOFF2 fonts, convert to TTF, embed in EPUB; solves lightnovel.app font obfuscation
- **Illustration embedding** — auto-download chapter images, rewrite `src` to local paths, embed in EPUB
- **Fetch memory** — per-chapter tracking; re-runs skip already-fetched chapters (great for ongoing serials)
- **`--all` auto-pack** — fetch all chapters then automatically pack into EPUB
- **`--pack-only`** — skip fetching, pack existing HTML files directly into EPUB
- **Encoding detection + chapter parsing** — smart TXT encoding detection, chapter title recognition, paragraph merging, noise removal
- **Pre-compiled Dart bridge** — no Dart SDK required, works out of the box (Windows)

## Quick Start

```bash
# 1. Extract txt-to-epub-dist.zip
# 2. One-click dependency install
setup.bat

# 3. Edit config.json, add your lightnovel.app refresh_token (optional; lightnovel source only)
#    Open lightnovel.app in browser → F12 → Application → Local Storage
#    Key: sb-yywiuxedvyfxdpznoyqy-auth-token

# 4. Start using
python ebook.py lightnovel --bid <BID> --all                       # fetch + auto-pack EPUB
python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"
python ebook.py syosetu -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>
python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "Title" --author "Author"
```

## Supported Sources

| Source | Method | Font EPUB | Illustrations | Requires |
|--------|--------|:---------:|:-------------:|----------|
| [lightnovel.app](https://www.lightnovel.app) | Dart SignalR bridge | ✅ | ✅ | refresh_token |
| [syosetu.org](https://syosetu.org) | CDP (Edge browser) | — | ✅ | Edge browser |
| [wenku8.net](https://www.wenku8.net) | CDP (Edge browser) | — | ✅ | Edge browser |
| [novelia.cc](https://novelia.cc) | REST API | — | — | — |
| Local TXT | stdlib | — | — | — |

## Installation

### Prerequisites

- Python 3.9+
- Microsoft Edge (for syosetu/wenku8 CDP fetchers; auto-detected)

### Setup

```bash
# Automatic (Windows)
setup.bat

# Manual
pip install -r requirements.txt
```

The Dart bridge is **pre-compiled** (`lightnovel_bridge.exe`). No Dart SDK installation needed.  
To recompile from source: `winget install Google.DartSDK` then `dart compile exe bin/lightnovel_bridge.dart`.

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
python ebook.py lightnovel --bid <BID> --pack-only                 # pack existing files
python ebook.py lightnovel --bid <BID> --pack-only --author "Author"

# syosetu.org
python ebook.py syosetu -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>

# wenku8.net
python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"

# novelia.cc
python ebook.py novelia "https://n.novelia.cc/novel/<SOURCE>/<ID>" -o <OUTPUT>.txt

# TXT → EPUB
python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "Title" --author "Author"

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

### Why Dart Bridge?

lightnovel.app's SignalR WebSocket server performs **TLS fingerprinting** — standard Python WebSocket libraries (`websockets`, `websocket-client`) are rejected. Dart's native `dart:io` HttpClient uses BoringSSL, matching the Novella mobile app's fingerprint.

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
| `scripts/lightnovel_api.py` | lightnovel.app Dart bridge API fetcher |
| `scripts/syosetu_fetch.py` | syosetu.org CDP fetcher (Cloudflare bypass) |
| `scripts/wenku8_fetch.py` | wenku8.net CDP fetcher |
| `scripts/web_fetch.py` | novelia.cc REST API fetcher |
| `scripts/html2epub_font.py` | HTML directory → font-embedded EPUB (WOFF2→TTF) |
| `scripts/lightnovel_decode.py` | Offline font decoder for snapshots |
| `scripts/fetch_memory.py` | Fetch memory persistence |
| `scripts/html2txt.py` | [experimental] VLM OCR pipeline |
| `scripts/build_decode_map.py` | [experimental] VLM OCR glyph mapping |
| `scripts/dart_bridge/` | Dart SignalR WebSocket bridge |

## Dependencies

```
requests  websocket-client  fonttools  brotli  curl_cffi  Pillow
```

All installable via `pip install -r requirements.txt`.  
Pre-compiled Dart bridge included — no Dart SDK required.

## Notes

- **Config security**: `config.json` contains your lightnovel.app refresh token. It is excluded from the distribution zip and git-ignored.
- **Memory durability**: Fetch memory is stored in `fetch_memory.json`. This file is personal and excluded from distribution.
- **Experimental OCR**: The VLM OCR font decoding path (`build_decode_map.py`, `html2txt.py`) is retained as a draft. It works but has accuracy issues with visually similar characters.
- **Cross-platform**: Primarily developed for Windows. The Dart bridge `.exe` is Windows-only; non-Windows users need Dart SDK to run `dart run`.

## License

MIT
