#!/usr/bin/env python3
"""
Syosetu.org fetcher via Chrome DevTools Protocol (CDP).
Automatically launches Edge with remote debugging, bypasses Cloudflare, fetches all chapters.

Usage:
  python syosetu_fetch.py                                    # auto-detect novel from browser
  python syosetu_fetch.py -u https://syosetu.org/novel/68239/   # specify URL
  python syosetu_fetch.py -u ... --start 1 --end 3              # fetch chapters 1-3 only
"""

import re
import sys
import io
import json
import time
import argparse
import urllib.request
import subprocess
import os
from pathlib import Path

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"


def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_chapter(novel_id, chapter, novel_title):
    try:
        data = _load_memory()
        key = f'syosetu:{novel_id}'
        if key not in data:
            data[key] = {'title': novel_title, 'chapters': {}, 'first_fetch': time.strftime('%Y-%m-%d %H:%M'), 'extra': {}}
        data[key]['chapters'][str(chapter)] = time.strftime('%Y-%m-%d %H:%M')
        data[key]['last_fetch'] = time.strftime('%Y-%m-%d %H:%M')
        _save_memory(data)
    except Exception:
        pass


def _get_fetched(novel_id):
    data = _load_memory()
    entry = data.get(f'syosetu:{novel_id}', {})
    return {int(k) for k in entry.get('chapters', {})}


if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    import websocket
except ImportError:
    print("Error: pip install websocket-client", file=sys.stderr)
    sys.exit(1)

CDP_HTTP = 'http://127.0.0.1:9222'

# Edge path from config, with auto-detection fallback
try:
    from config import get_edge_path as _get_edge_path
    EDGE_PATH = _get_edge_path()
except ImportError:
    _EDGE_DEFAULT = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    EDGE_PATH = _EDGE_DEFAULT if Path(_EDGE_DEFAULT).exists() else 'msedge'

# Cloudflare interstitial titles (EN + JP)
CF_TITLES = ['Just a moment...', 'しばらくお待ちください', '请稍候']


# ============================================================
#  Browser lifecycle
# ============================================================

def kill_edge():
    """Kill all Edge processes."""
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/f', '/im', 'msedge.exe'],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(1)


def launch_edge(url, visible=False):
    """Launch Edge with remote debugging enabled, opening the target URL.
    By default, window is placed off-screen (invisible to user but NOT headless)."""
    kill_edge()

    args = [
        EDGE_PATH,
        '--remote-debugging-port=9222',
        '--remote-allow-origins=*',
        '--new-window',
    ]
    if not visible:
        # Off-screen: real browser, real viewport, just not visible to user
        args += ['--window-position=-32000,-32000', '--window-size=1920,1080']
    args.append(url)

    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
    )
    mode = 'visible' if visible else 'off-screen'
    print(f"Edge launched ({mode}), opening {url}")


# ============================================================
#  CDP helpers
# ============================================================

def cdp_request(path):
    """Send a CDP HTTP request."""
    with urllib.request.urlopen(f'{CDP_HTTP}{path}', timeout=5) as resp:
        return json.loads(resp.read().decode())


def is_cdp_alive():
    """Check if Edge CDP is listening."""
    try:
        cdp_request('/json/version')
        return True
    except Exception:
        return False


def find_novel_tab(novel_pattern=None):
    """Find a tab on syosetu.org (or matching pattern). Return tab dict or None."""
    try:
        tabs = cdp_request('/json')
    except Exception:
        return None
    for t in tabs:
        url = t.get('url', '')
        if novel_pattern:
            if novel_pattern in url:
                return t
        elif 'syosetu.org/novel/' in url:
            return t
    return None


def wait_for_novel(novel_url, timeout=60):
    """Wait for Edge CDP to be alive and the novel page to load (Cloudflare passed).

    Returns the tab dict once the page is ready.
    """
    novel_pattern = re.search(r'(syosetu\.org/novel/\d+)', novel_url).group(1)
    start = time.time()

    while time.time() - start < timeout:
        if not is_cdp_alive():
            print(f"  [{int(time.time()-start)}s] Waiting for Edge to start...")
            time.sleep(2)
            continue

        tab = find_novel_tab(novel_pattern)
        if tab:
            url = tab.get('url', '')
            title = tab.get('title', '')
            is_cf = any(cf in title for cf in CF_TITLES)

            if not is_cf and novel_pattern in url:
                print(f"  [{int(time.time()-start)}s] Cloudflare passed! Title: {title[:60]}")
                return tab

            status = 'Cloudflare' if is_cf else 'Loading'
            print(f"  [{int(time.time()-start)}s] {status}: {title[:60]}")

        time.sleep(2)

    return None


# ============================================================
#  WebSocket / CDP commands
# ============================================================

def ws_connect(ws_url):
    """Connect to a CDP WebSocket."""
    return websocket.create_connection(ws_url, timeout=15, suppress_origin=True)


def cdp_eval(ws, expression, timeout=15, await_promise=False):
    """Send Runtime.evaluate and return the result value."""
    msg_id = int(time.time() * 1000) % 100000
    params = {'expression': expression, 'returnByValue': True}
    if await_promise:
        params['awaitPromise'] = True
    payload = {'id': msg_id, 'method': 'Runtime.evaluate', 'params': params}
    ws.send(json.dumps(payload))
    old_timeout = ws.gettimeout()
    ws.settimeout(timeout)
    try:
        while True:
            raw = ws.recv()
            resp = json.loads(raw)
            if resp.get('id') == msg_id:
                if 'error' in resp:
                    raise RuntimeError(f"CDP error: {resp['error']}")
                return resp.get('result', {}).get('result', {}).get('value', '')
    finally:
        ws.settimeout(old_timeout)


def page_navigate(ws, url):
    """Navigate current page and wait for result."""
    msg_id = int(time.time() * 1000) % 100000
    payload = {'id': msg_id, 'method': 'Page.navigate', 'params': {'url': url}}
    ws.send(json.dumps(payload))
    old_timeout = ws.gettimeout()
    ws.settimeout(20)
    try:
        while True:
            raw = ws.recv()
            resp = json.loads(raw)
            if resp.get('id') == msg_id:
                if 'error' in resp:
                    raise RuntimeError(f"Navigate error: {resp['error']}")
                break
    finally:
        ws.settimeout(old_timeout)
    time.sleep(0.5)


# ============================================================
#  Page content extraction
# ============================================================

def extract_text(ws):
    return cdp_eval(ws, 'document.body ? document.body.innerText : ""')


def extract_chapter_count(ws):
    text = extract_text(ws)
    m = re.search(r'(\d+)\s*/\s*(\d+)', text)
    if m:
        return int(m.group(2))
    return 0


def extract_chapter_title(ws):
    return cdp_eval(ws, '''
        (function() {
            // Try og:title meta tag first (most reliable)
            let og = document.querySelector('meta[property="og:title"]');
            if (og) {
                let t = og.getAttribute('content') || '';
                if (t && t.length > 2) return t;
            }
            // Try visible heading (exclude site-title logo links)
            let h = document.querySelector('h1:not(.siteTitle), .novel-title, .chapter-title, .novel h1');
            if (h && h.textContent.trim().length > 1) return h.textContent.trim().substring(0, 200);
            // Fallback to document.title
            let dt = document.title || '';
            // Strip " - ハーメルン" suffix
            dt = dt.replace(/\\s*-\\s*ハーメルン.*$/, '').trim();
            return dt.substring(0, 200);
        })()
    ''')


def extract_html(ws):
    """Extract the innerHTML of #honbun (the novel content area)."""
    return cdp_eval(ws, '''
        (function() {
            let h = document.querySelector('#honbun');
            return h ? h.innerHTML : '';
        })()
    ''')


def extract_novel_metadata(ws):
    """Extract novel title and other metadata from the page."""
    return cdp_eval(ws, '''
        (function() {
            let meta = {};
            // Title from og:title or site title
            let ogTitle = document.querySelector('meta[property=\"og:title\"]');
            if (ogTitle) meta.title = ogTitle.getAttribute('content');
            // Author - often in the TOC page or a specific element
            let author = document.querySelector('.author, [class*=\"author\"]');
            if (author) meta.author = author.textContent.trim();
            // Try the SS detail link
            let detailLink = document.querySelector('a[href*=\"ss_detail\"]');
            if (detailLink) {
                meta.detail_url = detailLink.href;
            }
            return JSON.stringify(meta);
        })()
    ''')


# ============================================================
#  Image extraction
# ============================================================

def download_images_via_cdp(ws, html, img_dir):
    """Download illustration images using CDP Network.getResponseBody.

    We enable Network domain, trigger <img> load in the browser, capture the
    requestId from Network.responseReceived events, then call getResponseBody.
    """
    img_dir = Path(img_dir)
    img_dir.mkdir(parents=True, exist_ok=True)
    images = []

    def _replace_link(m):
        tag = m.group(0)
        href_m = re.search(r'href="(http://syosetu\.org/[^"]+)"', tag)
        if not href_m:
            return tag
        href = href_m.group(1)
        img_url = href.replace('http://syosetu.org', 'https://img.syosetu.org')

        url_path = img_url.split('?')[0]
        fname = url_path.rsplit('/', 1)[-1]
        if not fname or '.' not in fname:
            fname = f"img_{abs(hash(img_url))}.png"

        local_path = img_dir / fname

        if not local_path.exists():
            try:
                import base64

                # Send Runtime.evaluate with awaitPromise AND listen for Network events
                eval_id = int(time.time() * 1000) % 100000
                ws.send(json.dumps({'id': eval_id, 'method': 'Runtime.evaluate', 'params': {
                    'expression': f'''
                    (function() {{
                        return new Promise((resolve) => {{
                            let img = document.createElement('img');
                            img.onload = function() {{ resolve('loaded'); }};
                            img.onerror = function() {{ resolve('error'); }};
                            img.src = '{img_url}';
                            document.body.appendChild(img);
                            setTimeout(() => resolve('timeout'), 8000);
                        }});
                    }})()
                    ''',
                    'returnByValue': True,
                    'awaitPromise': True,
                }}))

                # Listen for both the eval response AND Network.responseReceived
                req_id = None
                eval_done = False
                old_timeout = ws.gettimeout()
                ws.settimeout(15)
                for _ in range(50):
                    try:
                        raw = ws.recv()
                        msg = json.loads(raw)

                        if msg.get('id') == eval_id:
                            eval_done = True
                            if req_id:
                                break

                        if msg.get('method') == 'Network.responseReceived':
                            resp = msg.get('params', {}).get('response', {})
                            if resp.get('url') == img_url:
                                req_id = msg['params']['requestId']
                                if eval_done:
                                    break
                    except:
                        break
                ws.settimeout(old_timeout)

                # Get response body
                data = None
                if req_id:
                    gid = int(time.time() * 1000) % 100000
                    ws.send(json.dumps({'id': gid, 'method': 'Network.getResponseBody',
                        'params': {'requestId': req_id}}))
                    ws.settimeout(10)
                    for _ in range(20):
                        try:
                            r = json.loads(ws.recv())
                            if r.get('id') == gid:
                                body = r.get('result', {}).get('body', '')
                                base64_encoded = r.get('result', {}).get('base64Encoded', False)
                                if base64_encoded:
                                    data = base64.b64decode(body)
                                else:
                                    data = body.encode('latin-1')
                                break
                        except:
                            break
                    ws.settimeout(old_timeout)

                if data:
                    local_path.write_bytes(data)
                    print(f"    Image: {fname} ({len(data):,} bytes)", file=sys.stderr)
                else:
                    print(f"    Image FAILED: {fname} - no response body (req_id={req_id})", file=sys.stderr)
                    return tag
            except Exception as e:
                print(f"    Image FAILED: {fname} - {e}", file=sys.stderr)
                return tag

        images.append((img_url, fname, str(local_path)))
        return f'<img src="images/{fname}" alt="挿絵" style="max-width:100%;height:auto;margin:1em auto;display:block;">'

    new_html = re.sub(r'<a[^>]*href="http://syosetu\.org/[^"]*"[^>]*>【挿絵表示】</a>',
                      _replace_link, html)
    return new_html, images


# ============================================================
#  HTML output
# ============================================================

_HTML_PAGE = '''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{
    font-family: 'Yu Mincho', 'YuMincho', 'Hiragino Mincho Pro', serif;
    max-width: 800px;
    margin: 0 auto;
    padding: 2em 1.5em;
    line-height: 1.9;
    font-size: 18px;
    color: #333;
    background: #fdfdfd;
  }}
  p {{ text-indent: 1em; margin: 0.3em 0; }}
  h1 {{ text-indent: 0; }}
  img {{ max-width: 100%; height: auto; }}
</style>
</head>
<body>
<h1 style="text-align:center;font-size:1.4em;">{title}</h1>
{body}
</body>
</html>
'''


def save_html_chapter(html_dir, ch_num, ch_title, honbun_html, images):
    """Save a single chapter as a self-contained HTML file."""
    html_dir = Path(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)

    safe_title = re.sub(r'[<>:"/\\|?*]', '_', ch_title or f'chapter{ch_num}')
    fname = f"{ch_num:04d}_{safe_title}.html"
    path = html_dir / fname

    full_html = _HTML_PAGE.format(title=ch_title or f'Chapter {ch_num}', body=honbun_html)
    path.write_text(full_html, encoding='utf-8')
    print(f"    HTML: {fname}", file=sys.stderr)
    return fname


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Fetch syosetu.org novel (auto-launches Edge, bypasses Cloudflare)')
    parser.add_argument('-u', '--url', help='syosetu.org novel URL (e.g. https://syosetu.org/novel/68239/)')
    parser.add_argument('-o', '--output', default='syosetu_fetched.txt', help='Output TXT file (TXT mode)')
    parser.add_argument('--html', help='Output per-chapter HTML files to this directory (HTML mode)')
    parser.add_argument('-d', '--delay', type=float, default=0.8, help='Delay between chapters')
    parser.add_argument('--start', type=int, default=1, help='Start chapter (default: 1)')
    parser.add_argument('--end', type=int, default=0, help='End chapter (0=auto-detect all)')
    parser.add_argument('--no-launch', action='store_true',
                        help='Do not auto-launch Edge (connect to existing)')
    parser.add_argument('--visible', action='store_true',
                        help='Show Edge window (default: off-screen)')
    args = parser.parse_args()

    novel_url = args.url
    html_mode = bool(args.html)

    # Determine novel URL
    if not novel_url:
        tab = find_novel_tab()
        if tab:
            novel_url = tab['url']
            print(f"Found existing tab: {novel_url}")
        else:
            print("No URL specified and no existing syosetu tab found.")
            print("Usage: python syosetu_fetch.py -u https://syosetu.org/novel/68239/")
            sys.exit(1)

    base_url = re.match(r'(https?://[^/]+/novel/\d+)', novel_url).group(1)
    novel_id = re.search(r'/novel/(\d+)', novel_url).group(1)
    print(f"Novel: {base_url} (ID: {novel_id})")

    html_output_dir = None
    if html_mode:
        html_output_dir = Path(args.html)
        html_output_dir.mkdir(parents=True, exist_ok=True)

    # Auto-launch Edge if requested
    if not args.no_launch:
        if is_cdp_alive() and find_novel_tab():
            print("Edge already running with novel tab, reusing.")
        else:
            launch_edge(novel_url, visible=args.visible)
            print("Waiting for Cloudflare to pass...")
            tab = wait_for_novel(novel_url, timeout=60)
            if not tab:
                print("ERROR: Timeout waiting for page to load.")
                print("Edge may need manual interaction. Try without --no-launch?")
                sys.exit(1)
    else:
        tab = find_novel_tab()
        if not tab:
            print("ERROR: No syosetu tab found. Start Edge with --remote-debugging-port=9222")
            sys.exit(1)

    # Connect to the tab
    tab = find_novel_tab(novel_id)
    if not tab:
        print("ERROR: Lost syosetu tab!")
        sys.exit(1)

    ws_url = tab['webSocketDebuggerUrl']
    print(f"Connecting to tab: {tab['title'][:60]}")
    ws = ws_connect(ws_url)
    print("Connected!")

    # Navigate to first chapter to get total count
    first_url = f'{base_url}/1.html'
    print(f"Opening chapter 1...")
    page_navigate(ws, first_url)
    time.sleep(1)

    total = extract_chapter_count(ws)
    if args.end > 0:
        total = min(args.end, total)
    print(f"Total chapters: {total}")

    # Extract novel title from og:title or document.title
    # Format: "ChapterTitle - NovelTitle - ハーメルン" or "NovelTitle - ChapterTitle - ハーメルン"
    full_title = extract_chapter_title(ws)
    parts = [p.strip() for p in full_title.split(' - ')]
    # Remove ハーメルン suffix
    parts = [p for p in parts if 'ハーメルン' not in p and 'hameln' not in p.lower()]
    # Novel title is the part that does NOT contain chapter numbers
    novel_title = ''
    chapter_title = ''
    for p in parts:
        if re.search(r'第[一二三四五六七八九十百千\d]+[話話章]', p):
            chapter_title = p
        else:
            novel_title = p
    if not novel_title:
        novel_title = parts[0] if parts else full_title.strip()
    print(f"Title: {novel_title}")

    # Enable Network domain for image capture (HTML mode)
    if html_mode:
        ws.send(json.dumps({'id': 99999, 'method': 'Network.enable', 'params': {}}))
        for _ in range(10):
            try:
                r = json.loads(ws.recv())
                if r.get('id') == 99999:
                    break
            except:
                break

    # Fetch chapters
    if html_mode:
        img_dir = html_output_dir / 'images'
        chapters_info = []

        for i in range(args.start, total + 1):
            ch_url = f'{base_url}/{i}.html'
            print(f"  [{i}/{total}] ", end='', flush=True)

            if i > args.start:
                page_navigate(ws, ch_url)
                time.sleep(0.3)

            ch_title = extract_chapter_title(ws)
            ch_title = re.sub(r'\s*[-–—].*ハーメルン.*$', '', ch_title).strip()
            parts_ct = ch_title.split(' - ')
            if len(parts_ct) > 2:
                ch_title = parts_ct[0].strip()
            elif len(parts_ct) == 2 and re.search(r'第[一二三四五六七八九十百千\d]+[話話章]', parts_ct[0]):
                ch_title = parts_ct[0].strip()
            print(f"{ch_title[:50]}...", end=' ', flush=True)

            honbun = extract_html(ws)
            if honbun:
                honbun, images = download_images_via_cdp(ws, honbun, img_dir)
                fname = save_html_chapter(html_output_dir, i, ch_title, honbun, images)
                chapters_info.append({'num': i, 'title': ch_title, 'file': fname})
                print(f'OK (images: {len(images)})')
            else:
                print('EMPTY')

            time.sleep(args.delay)

        # Save metadata
        info = {
            'novel_id': int(novel_id),
            'title': novel_title,
            'author': '',
            'chapters': chapters_info,
        }
        info_path = html_output_dir / 'book_info.json'
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\nMetadata: {info_path}")
        print(f"HTML chapters saved to: {html_output_dir}")
        print(f"Total: {len(chapters_info)} chapters")
    else:
        parts = [f"# {novel_title}", '', '']

        for i in range(args.start, total + 1):
            ch_url = f'{base_url}/{i}.html'
            prefix = f"[{i}/{total}]"
            print(f"  {prefix} ", end='', flush=True)

            if i > args.start:
                page_navigate(ws, ch_url)
                time.sleep(0.3)

            ch_title = extract_chapter_title(ws)
            ch_title = re.sub(r'\s*[-–—].*ハーメルン.*$', '', ch_title).strip()
            parts_ct = ch_title.split(' - ')
            if len(parts_ct) > 2:
                ch_title = parts_ct[0].strip()
            elif len(parts_ct) == 2 and re.search(r'第[一二三四五六七八九十百千\d]+[話話章]', parts_ct[0]):
                ch_title = parts_ct[0].strip()
            print(f"{ch_title[:50]}...", end=' ', flush=True)

            content = extract_text(ws)
            content = re.sub(r'小説閲覧履歴.*?閲覧設定\n?', '', content, flags=re.DOTALL)
            content = re.sub(r'\n{3,}', '\n\n', content).strip()

            parts.append(f'\n\n第{i}章 {ch_title}\n')
            parts.append(content)
            print('OK')

            time.sleep(args.delay)

        combined = '\n'.join(parts)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(combined)
        print(f"\nSaved: {args.output} ({len(combined):,} chars)")

    print("Done!")
    # Mark all fetched chapters
    for i in range(args.start, total + 1):
        _mark_chapter(novel_id, i, novel_title)
    ws.close()


if __name__ == '__main__':
    main()
