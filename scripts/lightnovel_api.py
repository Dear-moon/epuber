#!/usr/bin/env python3
"""
Lightnovel.app full API pipeline — Dart WebSocket bridge for SignalR.
Server blocks non-Dart WebSocket TLS fingerprints; Dart's native BoringSSL passes.

Usage:
  1. Install Dart SDK: winget install Google.DartSDK
  2. Fill in REFRESH_TOKEN below (from browser → DevTools → Application → Local Storage →
     key: sb-yywiuxedvyfxdpznoyqy-auth-token)
  3. python lightnovel_api.py --bid 17028 --chapter 8
"""

import sys, io, json, re, subprocess, argparse, time
from pathlib import Path

# Import shared config
try:
    from config import get_refresh_token, get_fetch_dir
except ImportError:
    # Fallback: add parent dir to path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import get_refresh_token, get_fetch_dir

USER_AGENT = 'Novella/1.8.0'
MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"

if sys.platform == 'win32':
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except: pass

# REFRESH_TOKEN is now in config.json — see config.example.json for template
REFRESH_TOKEN = None  # Loaded lazily from config in main()

API_BASE = "https://api.lightnovel.life"
USER_AGENT = "Novella/1.8.0"

# Path to Dart SDK and bridge script
DART_EXE = None  # Auto-detect; set manually if needed
BRIDGE_SCRIPT = Path(__file__).parent / "dart_bridge" / "bin" / "lightnovel_bridge.dart"


def _find_dart():
    """Auto-detect Dart executable."""
    if DART_EXE:
        return DART_EXE

    import shutil
    # Check common locations
    candidates = [
        # winget install location
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages",
        # Direct PATH lookup
        shutil.which("dart"),
        shutil.which("dart.exe"),
    ]

    # Search winget packages for Dart SDK
    winget_pkg = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_pkg.exists():
        for d in winget_pkg.iterdir():
            if d.name.startswith("Google.DartSDK"):
                dart = d / "dart-sdk" / "bin" / "dart.exe"
                if dart.exists():
                    return str(dart)

    for c in candidates:
        if isinstance(c, str) and c:
            return c

    return None


def _fetch_chapter_via_dart(token, bid, chapter):
    """Call Dart bridge via subprocess to fetch chapter data.
    Prefers pre-compiled .exe; falls back to `dart run`."""
    bridge_exe = BRIDGE_SCRIPT.with_suffix('.exe')

    if bridge_exe.exists():
        # Pre-compiled standalone executable (no Dart SDK needed)
        cmd = [
            str(bridge_exe),
            "--token", token,
            "--bid", str(bid),
            "--chapter", str(chapter),
        ]
        print(f"Bridge (exe): {bridge_exe}", file=sys.stderr)
    else:
        dart = _find_dart()
        if not dart:
            raise RuntimeError(
                "Dart SDK not found. Install with: winget install Google.DartSDK\n"
                "Or set DART_EXE in the script."
            )
        if not BRIDGE_SCRIPT.exists():
            raise RuntimeError(f"Dart bridge script not found: {BRIDGE_SCRIPT}")
        cmd = [
            dart, "run", str(BRIDGE_SCRIPT),
            "--token", token,
            "--bid", str(bid),
            "--chapter", str(chapter),
        ]
        print(f"Dart: {dart}", file=sys.stderr)
        print(f"Bridge: {BRIDGE_SCRIPT}", file=sys.stderr)

    result = subprocess.run(
        cmd, capture_output=True, text=False, timeout=45,
        cwd=str(BRIDGE_SCRIPT.parent.parent),  # dart_bridge/ directory
    )

    # Dart prints logs to stderr, JSON to stdout
    stderr_text = result.stderr.decode('utf-8', errors='replace') if isinstance(result.stderr, bytes) else result.stderr
    if stderr_text:
        for line in stderr_text.strip().split('\n'):
            print(f"  [dart] {line}", file=sys.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"Dart bridge exited with code {result.returncode}")

    stdout_text = result.stdout.decode('utf-8', errors='replace') if isinstance(result.stdout, bytes) else result.stdout
    if not stdout_text.strip():
        raise RuntimeError("Dart bridge returned empty output")

    return json.loads(stdout_text.strip().split('\n')[-1])  # Last line is JSON


# ============================================================
#  Font download & decode (same as before)
# ============================================================

try:
    from curl_cffi import requests as curl_requests
    _has_curl = True
except ImportError:
    _has_curl = False


def _download_font(font_path):
    """Download a chapter font file. Uses curl_cffi if available."""
    url = f"{API_BASE}{font_path}"
    print(f"Downloading font: {url}", file=sys.stderr)

    if _has_curl:
        resp = curl_requests.get(url, impersonate='chrome124', timeout=30, verify=False)
        if resp.status_code == 200 and len(resp.content) > 1000:
            print(f"  {len(resp.content):,} bytes (curl_cffi)", file=sys.stderr)
            return resp.content

    # Fallback to urllib
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    resp = urllib.request.urlopen(req, context=ctx, timeout=30)
    data = resp.read()
    print(f"  {len(data):,} bytes (urllib)", file=sys.stderr)
    return data


def _extract_and_download_images(html, book_dir):
    """Extract <img> tags from HTML, download images, return (html_with_local_paths, image_list)."""
    import urllib.request as ulib

    img_dir = book_dir / 'images'
    img_dir.mkdir(parents=True, exist_ok=True)

    images = []  # [(original_url, local_filename, local_path)]

    def _replace_img(m):
        tag = m.group(0)
        src_m = re.search(r'src="([^"]+)"', tag)
        if not src_m:
            return tag
        src_orig = src_m.group(1)   # Keep original for replacement (may have &amp;)

        # Unescape HTML entities for download URL
        import html as _html
        src = _html.unescape(src_orig)

        # Resolve relative URLs
        if src.startswith('/'):
            src = f'https://api.lightnovel.life{src}'

        # Skip already-local paths
        if not src.startswith('http'):
            return tag

        # Determine local filename from URL
        url_path = src.split('?')[0]
        fname = url_path.rsplit('/', 1)[-1]
        if not fname or '.' not in fname:
            fname = f"img_{abs(hash(src))}.jpg"

        local_path = img_dir / fname

        # Download if not cached
        if not local_path.exists():
            try:
                # img.lightnovel.life rejects impersonated TLS; use requests
                import requests as _reqs
                resp = _reqs.get(src, headers={
                    'User-Agent': USER_AGENT,
                    'Referer': 'https://www.lightnovel.app/',
                    'Origin': 'https://www.lightnovel.app',
                }, verify=False, timeout=30)
                resp.raise_for_status()
                data = resp.content
                local_path.write_bytes(data)
                print(f"  Image: {fname} ({len(data):,} bytes)", file=sys.stderr)
            except Exception as e:
                print(f"  Image FAILED: {fname} - {e}", file=sys.stderr)
                return tag

        images.append((src, fname, str(local_path)))
        # Rewrite src to local path — must use original src value (with &amp; if present)
        return tag.replace(src_orig, f'images/{fname}')

    new_html = re.sub(r'<img[^>]+>', _replace_img, html)
    return new_html, images


try:
    from fontTools.ttLib import TTFont
    _has_fonttools = True
except ImportError:
    _has_fonttools = False


def decode_html(html, font_data):
    """Decode font-obfuscated HTML. Requires fonttools."""
    if not _has_fonttools:
        print("WARNING: fonttools not installed, skipping decode.", file=sys.stderr)
        text = re.sub(r'<[^>]+>', '', html)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return text, 0

    tmp = Path("_temp_font.woff2")
    tmp.write_bytes(font_data)
    try:
        font = TTFont(str(tmp))
        cmap = font["cmap"].getBestCmap()
        glyf = font["glyf"]
        hmtx = font["hmtx"]
        inv = set()
        for cp, name in cmap.items():
            if not cp: continue
            g = glyf.get(name)
            if not g: continue
            nc = g.numberOfContours if hasattr(g, 'numberOfContours') else -1
            adv = hmtx.metrics.get(name, (None,))[0]
            if adv == 0 and nc == 0: inv.add(cp)
        cleaned = ''.join(c for c in html if ord(c) not in inv)
        text = re.sub(r'<[^>]+>', '', cleaned)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return text, len(inv)
    finally:
        if tmp.exists(): tmp.unlink()


# ============================================================
#  HTML output (self-contained, readable in browser)
# ============================================================

def _clean_html(html, font_data):
    """Remove invisible codepoints from HTML, keep structure."""
    tmp = Path("_temp_font.woff2")
    tmp.write_bytes(font_data)
    try:
        font = TTFont(str(tmp))
        cmap = font["cmap"].getBestCmap()
        glyf = font["glyf"]
        hmtx = font["hmtx"]
        inv = set()
        for cp, name in cmap.items():
            if not cp: continue
            g = glyf.get(name)
            if not g: continue
            nc = g.numberOfContours if hasattr(g, 'numberOfContours') else -1
            adv = hmtx.metrics.get(name, (None,))[0]
            if adv == 0 and nc == 0: inv.add(cp)
        cleaned = ''.join(c for c in html if ord(c) not in inv)
        print(f"  Invisible codepoints removed: {len(inv)}", file=sys.stderr)
        return cleaned
    finally:
        if tmp.exists(): tmp.unlink()


_HTML_PLAIN = '''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: 'Yu Mincho', serif; max-width: 800px; margin: 0 auto;
         padding: 2em 1.5em; line-height: 1.9; font-size: 18px; }}
  p {{ text-indent: 1em; margin: 0.3em 0; }}
  .biaoti1 {{ font-size: 1.4em; font-weight: bold; text-align: center; margin: 1em 0; text-indent: 0; }}
  .biaoti2 {{ text-align: center; margin: 0.5em 0; font-size: 0.95em; text-indent: 0; }}
  .empty-line {{ height: 1em; text-indent: 0; }}
  img {{ max-width: 100%; height: auto; }}
</style></head>
<body>{body}</body></html>'''

_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  @font-face {{
    font-family: 'NovelFont';
    src: url(data:font/woff2;base64,{font_b64}) format('woff2');
  }}
  body {{
    font-family: 'NovelFont', serif;
    max-width: 800px;
    margin: 0 auto;
    padding: 2em 1.5em;
    line-height: 1.9;
    font-size: 18px;
    color: #333;
    background: #fdfdfd;
  }}
  p {{
    text-indent: 1em;
    margin: 0.3em 0;
  }}
  .biaoti1 {{
    font-size: 1.4em;
    font-weight: bold;
    text-align: center;
    margin: 1em 0;
  }}
  p {{
    text-indent: 2em;
    margin: 0.5em 0;
  }}
</style>
</head>
<body>
{content}
</body>
</html>'''


def _write_html(path, book_name, title, html, font_data):
    """Write a self-contained HTML file with embedded WOFF2 font."""
    import base64 as b64

    cleaned_html = _clean_html(html, font_data)
    font_b64 = b64.b64encode(font_data).decode('ascii')
    full_title = f"{book_name} - {title}" if book_name else title

    html_output = _HTML_TEMPLATE.format(
        title=full_title,
        font_b64=font_b64,
        content=cleaned_html,
    )

    Path(path).write_text(html_output, encoding='utf-8')
    print(f"  HTML: {len(html_output):,} chars ({len(font_b64):,} base64 font)", file=sys.stderr)


# ============================================================
#  Main
# ============================================================

def _mark_memory(bid, chapter, book_name):
    """Record this chapter in fetch memory."""
    try:
        data = {}
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
        key = f'lightnovel:{bid}'
        if key not in data:
            data[key] = {
                'title': book_name, 'chapters': {},
                'first_fetch': time.strftime('%Y-%m-%d %H:%M'), 'extra': {},
            }
        data[key]['chapters'][str(chapter)] = time.strftime('%Y-%m-%d %H:%M')
        data[key]['last_fetch'] = time.strftime('%Y-%m-%d %H:%M')
        MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass  # memory is non-critical


def main():
    parser = argparse.ArgumentParser(description='Lightnovel.app API chapter fetcher (Dart bridge)')
    parser.add_argument('--bid', type=int, required=True, help='Book ID')
    parser.add_argument('--chapter', type=int, required=True, help='Chapter SortNum')
    parser.add_argument('-o', '--output', help='Output file (text)')
    parser.add_argument('--html', nargs='?', const='__AUTO__',
                        help='Output self-contained HTML (auto-named if no path given)')
    parser.add_argument('--token', help='Refresh token (overrides config)')
    parser.add_argument('--raw-json', help='Save raw JSON from API (for debugging)')
    parser.add_argument('--no-memory', action='store_true', help='Skip fetch memory recording')
    args = parser.parse_args()

    token = args.token or get_refresh_token()
    if not token:
        print("ERROR: No refresh token. Set lightnovel.refresh_token in config.json or use --token",
              file=sys.stderr)
        print("Get it from: DevTools → Application → Local Storage → lightnovel.app",
              file=sys.stderr)
        print("Key: sb-yywiuxedvyfxdpznoyqy-auth-token", file=sys.stderr)
        sys.exit(1)

    fetch_dir = get_fetch_dir()

    # Step 1: Fetch chapter via Dart bridge
    print(f"\nFetching book {args.bid} chapter {args.chapter} via Dart bridge...", file=sys.stderr)
    try:
        data = _fetch_chapter_via_dart(token, args.bid, args.chapter)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.raw_json:
        Path(args.raw_json).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"Raw JSON saved: {args.raw_json}", file=sys.stderr)

    ch = data.get('Chapter', {})
    if not ch:
        print(f"ERROR: No Chapter data. Keys: {list(data.keys())}", file=sys.stderr)
        sys.exit(1)

    title = ch.get('Title', '?')
    html = ch.get('Content', '')
    font_path = ch.get('Font', '')
    book_name = ch.get('BookName', '')

    print(f"\nBook: {book_name}", file=sys.stderr)
    print(f"Chapter: {title}", file=sys.stderr)
    print(f"HTML: {len(html)} chars", file=sys.stderr)
    print(f"Font: {font_path}", file=sys.stderr)

    if not html:
        print("No chapter content", file=sys.stderr)
        sys.exit(1)

    # Build output directory early (needed for images + metadata)
    safe_book = re.sub(r'[<>:"/\\|?*]', '_', book_name or 'unknown')
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title or f'chapter{args.chapter}')
    book_dir = fetch_dir / safe_book
    book_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{args.chapter:04d}_{safe_title}"

    # Step 2: Download images referenced in chapter content
    html, images = _extract_and_download_images(html, book_dir)

    # Step 3: Save metadata (book_info.json)
    info_path = book_dir / 'book_info.json'
    if not info_path.exists():
        ch_list = ch.get('Chapters', [])
        info = {
            'book_id': args.bid,
            'book_name': book_name,
            'chapters': ch_list,
            'author': '',
            'cover': images[0][1] if images and 'cover' in images[0][0].lower() else '',
        }
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')

    # Step 4: Check for decode map
    decode_map = None
    map_path = book_dir / 'font_decode_map.json'
    if map_path.exists():
        try:
            decode_map = json.loads(map_path.read_text(encoding='utf-8'))
            decode_map = decode_map.get('map', {})
            if decode_map:
                print(f"  Decode map loaded: {len(decode_map)} entries", file=sys.stderr)
        except Exception as e:
            print(f"  Decode map load failed: {e}", file=sys.stderr)
            decode_map = None

    # Step 5: Download font (needed to remove invisible codepoints even with decode map)
    font_data = None
    if font_path:
        try:
            font_data = _download_font(font_path)
        except Exception as e:
            print(f"Font download failed: {e}", file=sys.stderr)

    # Step 6: Decode obfuscated text if map available
    decoded = False
    if decode_map and font_data:
        # Remove invisible codepoints first (same as original logic)
        import io as _io
        tmp = Path("_temp_font.woff2")
        tmp.write_bytes(font_data)
        try:
            _font = TTFont(str(tmp))
            _cmap = _font["cmap"].getBestCmap()
            _glyf = _font["glyf"]
            _hmtx = _font["hmtx"]
            _inv = set()
            for _cp, _name in _cmap.items():
                if not _cp: continue
                _g = _glyf.get(str(_name))
                if not _g: continue
                _nc = _g.numberOfContours if hasattr(_g, 'numberOfContours') else -1
                _adv = _hmtx.metrics.get(str(_name), (None,))[0]
                if _adv == 0 and _nc == 0: _inv.add(_cp)
            # Decode: map each char through decode_map
            html = ''.join(chr(decode_map.get(str(ord(c)), ord(c))) for c in html if ord(c) not in _inv)
            decoded = True
            print(f"  Text decoded: {len(decode_map)} chars mapped", file=sys.stderr)
        finally:
            _font.close()
            if tmp.exists(): tmp.unlink()

    # Step 7: Output
    if decoded:
        # Already decoded to readable Unicode — output plain HTML (no font needed)
        clean = re.sub(r'<[^>]+>', '', html)
        clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
        header = f"{book_name} - {title}\n\n" if book_name else ""
        output_text = header + clean

        if args.html is not None:
            plain_html = _HTML_PLAIN.format(title=title, body=html)
            if args.html == '__AUTO__':
                html_path = book_dir / f"{base_name}.html"
            else:
                html_path = Path(args.html)
            html_path.write_text(plain_html, encoding='utf-8')
            print(f"HTML (decoded) saved: {html_path}", file=sys.stderr)
        else:
            txt_path = book_dir / f"{base_name}.txt"
            txt_path.write_text(output_text, encoding='utf-8')
            print(f"TXT (decoded) saved: {txt_path}", file=sys.stderr)
    elif args.html is not None:
        # Obfuscated mode: embed font in HTML
        if not font_data:
            print("ERROR: --html requires font download to succeed", file=sys.stderr)
            sys.exit(1)

        if args.html == '__AUTO__':
            html_path = book_dir / f"{base_name}.html"
        else:
            html_path = Path(args.html)
        _write_html(str(html_path), book_name, title, html, font_data)
        print(f"HTML saved: {html_path}", file=sys.stderr)
        print("Open this file in a browser to read.", file=sys.stderr)
    else:
        if font_data:
            text, inv_count = decode_html(html, font_data)
            print(f"Decoded: {len(text)} chars ({inv_count} invisible removed)", file=sys.stderr)
        else:
            text = re.sub(r'<[^>]+>', '', html)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()

        header = f"{book_name} - {title}\n\n" if book_name else ""
        output = header + text

        if args.output:
            Path(args.output).write_text(output, encoding='utf-8')
            print(f"Saved: {args.output}", file=sys.stderr)
        else:
            txt_path = book_dir / f"{base_name}.txt"
            txt_path.write_text(output, encoding='utf-8')
            print(f"Saved: {txt_path}", file=sys.stderr)

    # Mark in fetch memory
    if not args.no_memory:
        _mark_memory(args.bid, args.chapter, book_name)


if __name__ == '__main__':
    main()
