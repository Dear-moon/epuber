#!/usr/bin/env python3
"""
Wenku8.net fetcher via CDP. Auto-launches Edge, bypasses Cloudflare.
Supports TXT mode and HTML mode (with illustration downloading).

Usage:
  python wenku8_fetch.py -u https://www.wenku8.net/novel/3/3988/index.htm
  python wenku8_fetch.py -u ... --html "D:/output/dir"   # HTML mode with illustrations
  python wenku8_fetch.py -u ... --start 1 --end 5
  python wenku8_fetch.py -u ... --visible
"""

import re, sys, io, json, time, argparse, urllib.request, subprocess
from pathlib import Path

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"


def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_chapter(novel_id, ch_id, novel_title):
    """Mark a wenku8 chapter as fetched. ch_id is the URL ID like '165421'."""
    try:
        data = _load_memory()
        key = f'wenku8:{novel_id}'
        if key not in data:
            data[key] = {'title': novel_title, 'chapters': {}, 'first_fetch': time.strftime('%Y-%m-%d %H:%M'), 'extra': {}}
        data[key]['chapters'][str(ch_id)] = time.strftime('%Y-%m-%d %H:%M')
        data[key]['last_fetch'] = time.strftime('%Y-%m-%d %H:%M')
        _save_memory(data)
    except Exception:
        pass


def _get_fetched(novel_id):
    data = _load_memory()
    entry = data.get(f'wenku8:{novel_id}', {})
    return set(entry.get('chapters', {}).keys())


try:
    import requests
except ImportError:
    requests = None

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

CF_TITLES = ['Just a moment...', 'しばらくお待ちください', '请稍候']


# ============================================================
#  Browser lifecycle
# ============================================================

def kill_edge():
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/f', '/im', 'msedge.exe'],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(1)


def launch_edge(url, visible=False):
    kill_edge()
    args = [
        EDGE_PATH,
        '--remote-debugging-port=9222',
        '--remote-allow-origins=*',
        '--new-window',
    ]
    if not visible:
        args += ['--window-position=-32000,-32000', '--window-size=1920,1080']
    args.append(url)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    mode = 'visible' if visible else 'off-screen'
    print(f"Edge launched ({mode}), opening {url}")


# ============================================================
#  CDP helpers
# ============================================================

def cdp_request(path):
    with urllib.request.urlopen(f'{CDP_HTTP}{path}', timeout=5) as resp:
        return json.loads(resp.read().decode())


def is_cdp_alive():
    try:
        cdp_request('/json/version')
        return True
    except Exception:
        return False


def find_tab(pattern):
    try:
        for t in cdp_request('/json'):
            if pattern in t.get('url', ''):
                return t
    except Exception:
        pass
    return None


def wait_for_page(url_pattern, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        if not is_cdp_alive():
            time.sleep(2)
            continue
        tab = find_tab(url_pattern)
        if tab:
            title = tab.get('title', '')
            is_cf = any(cf in title for cf in CF_TITLES)
            if not is_cf:
                elapsed = int(time.time() - start)
                print(f"  [{elapsed}s] Cloudflare passed! Title: {title[:60]}")
                return tab
            print(f"  [{int(time.time()-start)}s] Cloudflare: {title[:50]}")
        time.sleep(2)
    return None


def ws_connect(ws_url):
    return websocket.create_connection(ws_url, timeout=15, suppress_origin=True)


def cdp_eval(ws, expression, timeout=15, await_promise=False):
    msg_id = int(time.time() * 1000) % 100000
    params = {'expression': expression, 'returnByValue': True}
    if await_promise:
        params['awaitPromise'] = True
    ws.send(json.dumps({'id': msg_id, 'method': 'Runtime.evaluate', 'params': params}))
    old = ws.gettimeout()
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
        ws.settimeout(old)


def page_navigate(ws, url):
    msg_id = int(time.time() * 1000) % 100000
    ws.send(json.dumps({'id': msg_id, 'method': 'Page.navigate', 'params': {'url': url}}))
    old = ws.gettimeout()
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
        ws.settimeout(old)
    time.sleep(0.5)


# ============================================================
#  Wenku8-specific logic
# ============================================================

def extract_chapter_links(ws):
    """Extract chapter links from the index page. Returns list of (href, text) tuples."""
    result = cdp_eval(ws, """
        JSON.stringify(
            Array.from(document.querySelectorAll("td a")).filter(function(a) {
                return a.getAttribute("href") && a.getAttribute("href").endsWith(".htm");
            }).map(function(a) {
                return {href: a.getAttribute("href"), text: a.textContent.trim().substring(0, 100)};
            }).filter(function(x) { return x.text.length > 0; })
        )
    """)
    links = json.loads(result)
    seen = set()
    unique = []
    for l in links:
        href = l['href']
        if href not in seen:
            seen.add(href)
            unique.append((href, l['text']))
    return unique


def extract_novel_title(ws):
    result = cdp_eval(ws, '''
        (function() {
            let el = document.querySelector('#title');
            if (el) return el.textContent.trim();
            let body = document.body.innerText;
            let m = body.match(/^([^\\n]+)/);
            return m ? m[1].trim() : document.title.split('-')[0].trim();
        })()
    ''')
    return re.sub(r'\s*小说在线阅读.*$', '', result).strip()


def extract_author(ws):
    result = cdp_eval(ws, '''
        (function() {
            let infoDiv = document.querySelector('#info');
            if (infoDiv) {
                let m = infoDiv.textContent.match(/作者[：:]\\s*(\\S+)/);
                if (m) return m[1];
            }
            let body = document.body.innerText;
            let m = body.match(/作者[：:]\\s*(\\S+)/);
            return m ? m[1] : '';
        })()
    ''')
    return result


def extract_chapter_content(ws):
    """Extract novel content from a chapter page, returning HTML."""
    # Wait for content to load
    for _ in range(10):
        check = cdp_eval(ws,
            'document.querySelector("#content") ? document.querySelector("#content").innerText.length : 0',
            timeout=5)
        if check and int(check) > 50:
            break
        time.sleep(0.3)

    return cdp_eval(ws, '''
        (function() {
            let c = document.querySelector('#content');
            if (c) return c.innerHTML.trim();
            return '';
        })()
    ''')


def extract_image_links(ws):
    """Extract illustration image URLs from a page. Tries multiple selectors."""
    imgs = cdp_eval(ws, '''
        JSON.stringify(
            Array.from(document.querySelectorAll('img')).map(img => img.src)
                .filter(src => src.startsWith('http') && !src.includes('wenku8.net') && !src.includes('444495') && !src.includes('609999'))
        )
    ''')
    urls = json.loads(imgs) if isinstance(imgs, str) else []
    # Also try links that point to images
    if not urls:
        links = cdp_eval(ws, '''
            JSON.stringify(
                Array.from(document.querySelectorAll('a')).map(a => a.href)
                    .filter(href => href.includes('.jpg') || href.includes('.png') || href.includes('.gif'))
            )
        ''')
        urls = json.loads(links) if isinstance(links, str) else []
    return urls


# ============================================================
#  Image download
# ============================================================

def download_images(img_urls, img_dir):
    """Download images via plain HTTP. pic.777743.xyz just needs Referer header."""
    if not requests:
        print("    WARNING: pip install requests (image download skipped)", file=sys.stderr)
        return []
    img_dir = Path(img_dir)
    img_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    for img_url in img_urls:
        fname = img_url.rsplit('/', 1)[-1].split('?')[0]
        if not fname or '.' not in fname:
            fname = f"img_{abs(hash(img_url))}.jpg"
        local_path = img_dir / fname

        if local_path.exists():
            downloaded.append(fname)
            print(f"    Image (cached): {fname}", file=sys.stderr)
            continue

        try:
            resp = requests.get(img_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.wenku8.net/',
            }, verify=False, timeout=30)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            downloaded.append(fname)
            print(f"    Image: {fname} ({len(resp.content):,} bytes)", file=sys.stderr)
        except Exception as e:
            print(f"    Image FAILED: {fname} - {e}", file=sys.stderr)

    return downloaded


# ============================================================
#  HTML output
# ============================================================

_HTML_PAGE = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: "Microsoft YaHei", "SimSun", "Noto Serif CJK SC", "Yu Mincho", serif;
           max-width: 800px; margin: 0 auto; padding: 2em 1.5em;
           line-height: 1.9; font-size: 18px; }}
    p {{ text-indent: 1em; margin: 0.3em 0; }}
    h1 {{ text-align: center; font-size: 1.4em; text-indent: 0; }}
    img {{ max-width: 100%; height: auto; display: block; margin: 1em auto; }}
  </style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>'''


def save_html_chapter(html_dir, ch_num, ch_title, body_html, images=None):
    html_dir = Path(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', ch_title or f'chapter{ch_num}')
    fname = f"{ch_num:04d}_{safe_title}.html"
    path = html_dir / fname
    full = _HTML_PAGE.format(title=ch_title or f'Chapter {ch_num}', body=body_html)
    path.write_text(full, encoding='utf-8')
    return fname


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Fetch wenku8.net novel via CDP')
    parser.add_argument('-u', '--url', required=True, help='Index page URL')
    parser.add_argument('-o', '--output', help='Output TXT file')
    parser.add_argument('--html', help='Output per-chapter HTML files to this directory')
    parser.add_argument('-d', '--delay', type=float, default=1.0, help='Delay between chapters')
    parser.add_argument('--start', type=int, default=1, help='Start chapter index')
    parser.add_argument('--end', type=int, default=0, help='End chapter index (0=all)')
    parser.add_argument('--visible', action='store_true', help='Show browser window')
    args = parser.parse_args()

    index_url = args.url
    html_mode = bool(args.html)

    is_book_page = bool(re.match(r'https?://[^/]+/book/\d+\.html?', index_url))
    if is_book_page:
        novel_id = re.match(r'https?://[^/]+/book/(\d+)\.html?', index_url).group(1)
        site_pattern = f'wenku8.net/book/{novel_id}'
    else:
        site_pattern = re.search(r'(wenku8\.net/novel/\d+/\d+)', index_url).group(1)

    # Launch or reuse browser
    if is_cdp_alive() and find_tab(site_pattern):
        print("Reusing existing Edge tab.")
        tab = find_tab(site_pattern)
    else:
        launch_edge(index_url, visible=args.visible)
        print("Waiting for Cloudflare to pass...")
        tab = wait_for_page(site_pattern, timeout=60)
        if not tab:
            print("ERROR: Timeout waiting for page.")
            sys.exit(1)

    ws_url = tab['webSocketDebuggerUrl']
    ws = ws_connect(ws_url)
    print("Connected!")

    # If /book/ URL, find the TOC link and navigate there
    if is_book_page:
        time.sleep(2)
        toc_link = cdp_eval(ws,
            "(function(){var a=document.querySelectorAll('a');for(var i=0;i<a.length;i++){if(a[i].textContent.includes('目')&&a[i].href.includes('index'))return a[i].href}return''})()",
            timeout=5)
        if toc_link:
            toc_link = toc_link.strip('"\'')
            index_url = toc_link
            print(f"Auto TOC: {index_url}")
        page_navigate(ws, index_url)
        time.sleep(2)

    base_url = re.match(r'(https?://[^/]+/novel/\d+/\d+)', index_url).group(1)
    site_pattern = re.search(r'(wenku8\.net/novel/\d+/\d+)', index_url).group(1)
    novel_id = site_pattern.split('/')[-1]
    novel_cat = site_pattern.split('/')[-2]
    print(f"Novel base: {base_url}")

    # Wait for DOM to fully render (Cloudflare pass ≠ DOM ready)
    time.sleep(3)
    for _ in range(10):
        check = cdp_eval(ws, 'document.querySelectorAll("td a").length', timeout=5)
        if check and int(check) > 0:
            break
        time.sleep(1)

    # Extract metadata
    novel_title = extract_novel_title(ws)
    author = extract_author(ws)
    print(f"Title: {novel_title}")
    print(f"Author: {author}")

    # Extract chapter links
    print("Extracting chapter links...")
    chapter_links = extract_chapter_links(ws)
    print(f"Found {len(chapter_links)} chapters")

    if not chapter_links:
        print("No chapter links found!")
        ws.close()
        sys.exit(1)

    # Apply start/end
    total = len(chapter_links)
    start_idx = max(0, args.start - 1)
    end_idx = min(total, args.end) if args.end > 0 else total
    chapter_links = chapter_links[start_idx:end_idx]
    print(f"Fetching {len(chapter_links)} chapters ({args.start}-{start_idx + len(chapter_links)})")

    # Setup HTML mode
    html_output_dir = None
    img_dir = None
    cover_file = ''
    if html_mode:
        html_output_dir = Path(args.html)
        html_output_dir.mkdir(parents=True, exist_ok=True)
        img_dir = html_output_dir / 'images'
        img_dir.mkdir(parents=True, exist_ok=True)
        chapters_info = []

        # Download cover image
        cover_url = f'https://img.wenku8.com/image/{novel_cat}/{novel_id}/{novel_id}s.jpg'
        try:
            resp = requests.get(cover_url, headers={
                'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.wenku8.net/',
            }, verify=False, timeout=15)
            if resp.status_code == 200:
                cover_file = f'{novel_id}s.jpg'
                (img_dir / cover_file).write_bytes(resp.content)
                print(f"Cover: {cover_file} ({len(resp.content):,} bytes)")
            else:
                print(f"Cover: not found ({resp.status_code})")
        except Exception as e:
            print(f"Cover: download failed - {e}")

    parts = [f"# {novel_title}", f"作者: {author}", '', '']
    fetched_set = _get_fetched(novel_id) if not args.__dict__.get('force', False) else set()

    for i, (href, ch_title) in enumerate(chapter_links):
        ch_num = start_idx + i + 1
        ch_id = href.replace('.htm', '').split('/')[-1]
        full_url = href if href.startswith('http') else f'{base_url}/{href}'
        is_illust = '插图' in ch_title or '插畫' in ch_title or 'イラスト' in ch_title

        prefix = f"  [{ch_num}/{total}]"
        if ch_id in fetched_set:
            print(f"{prefix} (skip)", flush=True)
            continue
        print(f"{prefix} {ch_title[:50]}...", end=' ', flush=True)

        # Retry up to 3 times on connection failures
        for attempt in range(3):
            try:
                page_navigate(ws, full_url)
                break
            except Exception as e:
                if attempt < 2:
                    print(f'(retry {attempt+2})', end=' ', flush=True)
                    time.sleep(2 * (attempt + 1))
                    # Reconnect if needed
                    try: ws.close()
                    except: pass
                    try:
                        tabs = json.loads(urllib.request.urlopen(f'{CDP_HTTP}/json', timeout=5).read())
                        for t in tabs:
                            if 'wenku8' in t.get('url', ''):
                                ws = ws_connect(t['webSocketDebuggerUrl'])
                                break
                    except: pass
                else:
                    raise

        try:

            if html_mode and is_illust:
                # Illustration page: download images
                img_urls = extract_image_links(ws)
                if img_urls:
                    print(f"({len(img_urls)} images)", end=' ', flush=True)
                    downloaded = download_images(img_urls, img_dir)
                    imgs_html = ''.join(
                        f'<img src="images/{f}" alt="插图" style="max-width:100%;height:auto;display:block;margin:1em auto;">\n'
                        for f in downloaded
                    )
                    body_html = f'<div class="illustrations">{imgs_html}</div>'
                else:
                    body_html = '<p>（无插图）</p>'
                fname = save_html_chapter(html_output_dir, ch_num, ch_title, body_html)
                chapters_info.append({'num': ch_num, 'title': ch_title, 'file': fname, 'type': 'illustration'})
                print(f"OK ({len(downloaded) if img_urls else 0} imgs)")

            elif html_mode:
                # Text chapter: save as HTML
                content_html = extract_chapter_content(ws)
                # Clean watermarks/ads only — keep translator credits
                content_html = re.sub(r'<[^>]*>本文来自.*?轻小说文库\(.*?</[^>]*>\s*', '', content_html, flags=re.DOTALL)
                content_html = re.sub(r'最新最全的日本动漫轻小说.*?一网打尽！\s*', '', content_html)
                content_html = re.sub(r'轻小说文库.*?內容報錯\s*', '', content_html)
                content_html = re.sub(r'<div[^>]*id="[^"]*ad[^"]*"[^>]*>.*?</div>', '', content_html, flags=re.DOTALL)
                # XHTML fixes
                content_html = re.sub(r'&nbsp;', '&#160;', content_html)
                content_html = re.sub(r'<br\b([^>]*)>', r'<br\1/>', content_html)
                content_html = re.sub(r'<hr\b([^>]*)>', r'<hr\1/>', content_html)
                content_html = re.sub(r'<img\b([^>]*[^/])>', r'<img\1/>', content_html)
                body_html = f'<div class="chapter">{content_html}</div>'
                fname = save_html_chapter(html_output_dir, ch_num, ch_title, body_html)
                chapters_info.append({'num': ch_num, 'title': ch_title, 'file': fname, 'type': 'chapter'})
                print('OK')

            else:
                # TXT mode
                content = cdp_eval(ws, '''
                    (function() {
                        let c = document.querySelector('#content');
                        if (c) return c.innerText.trim();
                        return '';
                    })()
                ''')
                # Remove watermarks only — keep translation credits
                content = re.sub(r'本文来自 轻小说文库\(http://www\.wenku8\.com\).*?\n', '', content)
                content = re.sub(r'最新最全的日本动漫轻小说.*?一网打尽！\s*', '', content)
                content = re.sub(r'轻小说文库.*?內容報錯\s*', '', content)
                content = re.sub(r'背景颜色.*?最快）\s*', '', content, flags=re.DOTALL)
                content = re.sub(r'\n{3,}', '\n\n', content).strip()
                parts.append(f'\n\n第{ch_num}章 {ch_title}\n')
                parts.append(content)
                print('OK')

        except Exception as e:
            print(f'FAILED: {e}')
            if not html_mode:
                parts.append(f'\n\n第{ch_num}章 {ch_title}\n[获取失败: {e}]')

        time.sleep(args.delay)

    # Write output
    if html_mode:
        # Prepend cover page as first chapter
        if cover_file:
            cover_body = f'<div style="text-align:center;padding:2em 0;"><img src="images/{cover_file}" alt="封面" style="max-width:100%;height:auto;"/></div>'
            fname = save_html_chapter(html_output_dir, 0, '封面', cover_body)
            chapters_info.insert(0, {'num': 0, 'title': '封面', 'file': fname, 'type': 'cover'})

        info = {
            'book_name': novel_title,   # key matches html2epub_font.py expectation
            'author': author,
            'cover': cover_file,
            'chapters': chapters_info,
        }
        info_path = html_output_dir / 'book_info.json'
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\nMetadata: {info_path}")
        print(f"HTML chapters saved to: {html_output_dir}")
        print(f"Total: {len(chapters_info)} chapters")
        print("Next: python html2epub_font.py \"{html_output_dir}\" -o \"output.epub\"")
    else:
        if not args.output:
            safe = re.sub(r'[^\w一-鿿]', '_', novel_title).strip('_') or 'wenku8'
            args.output = f'{safe}.txt'
        combined = '\n'.join(parts)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(combined)
        print(f"\nSaved: {args.output} ({len(combined):,} chars)")

    print("Done!")
    # Mark per-chapter
    for href, _ in chapter_links:
        ch_id = href.replace('.htm', '').split('/')[-1]
        _mark_chapter(novel_id, ch_id, novel_title)
    ws.close()


if __name__ == '__main__':
    main()
