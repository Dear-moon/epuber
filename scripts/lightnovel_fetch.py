#!/usr/bin/env python3
"""
Lightnovel.app chapter fetcher — CDP page navigation + font decode.
Edge must be running with --remote-debugging-port=9222 and logged into lightnovel.app.

Usage:
  python lightnovel_fetch.py --bid 17028 --chapter 8
  python lightnovel_fetch.py --bid 17028 --chapter 8 -o chapter.txt
"""

import sys, io, json, time, re, base64, argparse
from pathlib import Path

if sys.platform == 'win32':
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except: pass

try:
    import urllib.request
    import websocket as _ws
except ImportError:
    print("Error: pip install websocket-client", file=sys.stderr); sys.exit(1)

try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: pip install fonttools brotli", file=sys.stderr); sys.exit(1)

CDP = 'http://127.0.0.1:9222'
READ_PAGE = 'https://www.lightnovel.app/read/{bid}/{sortnum}'
FONT_URL_RE = re.compile(r'https://api\.lightnovel\.life/font/[a-f0-9]+\.woff2')

# ============================================================
#  CDP helpers
# ============================================================

def _cdp_get(path):
    with urllib.request.urlopen(f'{CDP}{path}', timeout=5) as r:
        return json.loads(r.read().decode())

def _find_tab(pattern='lightnovel.app'):
    for t in _cdp_get('/json'):
        if t['type'] == 'page' and pattern in t.get('url', ''):
            return t
    return None

def _cdp_connect(tab):
    return _ws.create_connection(
        tab['webSocketDebuggerUrl'], timeout=15, suppress_origin=True)

def _cdp_eval(ws, expr, timeout=12, await_promise=False):
    mid = int(time.time() * 1000) % 100000
    params = {'expression': expr, 'returnByValue': True}
    if await_promise: params['awaitPromise'] = True
    ws.send(json.dumps({'id': mid, 'method': 'Runtime.evaluate', 'params': params}))
    ws.settimeout(timeout)
    for _ in range(500):
        raw = ws.recv()
        resp = json.loads(raw)
        if resp.get('id') == mid:
            return resp['result']['result'].get('value', '')
    return ''

def _cdp_navigate(ws, url):
    ws.send(json.dumps({'id': 1, 'method': 'Page.navigate', 'params': {'url': url}}))
    ws.settimeout(10)
    for _ in range(50):
        try:
            if json.loads(ws.recv()).get('id') == 1: return True
        except: return False
    return False

# ============================================================
#  Font download (curl_cffi preferred, browser fetch fallback)
# ============================================================

def _download_font_curl(url):
    """Download font via curl_cffi with Chrome TLS impersonation."""
    try:
        from curl_cffi import requests
    except ImportError:
        return None
    try:
        r = requests.get(url, impersonate='chrome124', timeout=30)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except Exception:
        pass
    return None

def _download_font_browser(ws, url):
    """Download font via browser fetch (CDP context)."""
    b64 = _cdp_eval(ws, f'''
    (async () => {{
        let r = await fetch("{url}");
        if (!r.ok) return "";
        let buf = await r.arrayBuffer();
        let bytes = new Uint8Array(buf);
        let bin = "";
        for (let b of bytes) bin += String.fromCharCode(b);
        return btoa(bin);
    }})()
    ''', timeout=20, await_promise=True)
    if b64 and len(b64) > 100:
        return base64.b64decode(b64)
    return None

def _get_font(ws, font_url):
    """Try curl_cffi first, fall back to browser fetch."""
    data = _download_font_curl(font_url)
    if data:
        print(f"  Font via curl_cffi: {len(data):,} bytes", file=sys.stderr)
        return data
    data = _download_font_browser(ws, font_url)
    if data:
        print(f"  Font via browser: {len(data):,} bytes", file=sys.stderr)
        return data
    return None

# ============================================================
#  Font decode
# ============================================================

def _decode(html, font_data):
    """Remove invisible codepoints and strip HTML."""
    tmp = Path('_temp_font.woff2')
    tmp.write_bytes(font_data)
    try:
        f = TTFont(str(tmp))
        cmap = f["cmap"].getBestCmap()
        glyf = f["glyf"]
        hmtx = f["hmtx"]
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
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Fetch lightnovel.app chapter via CDP')
    parser.add_argument('--bid', type=int, required=True, help='Book ID')
    parser.add_argument('--chapter', type=int, required=True, help='Chapter SortNum')
    parser.add_argument('-o', '--output', help='Output text file')
    args = parser.parse_args()

    # 1. Connect to Edge CDP
    tab = _find_tab()
    if not tab:
        print("ERROR: No lightnovel.app tab. Open Edge with:", file=sys.stderr)
        print(r'  msedge --remote-debugging-port=9222 --remote-allow-origins=*', file=sys.stderr)
        print("  Then log into lightnovel.app", file=sys.stderr)
        sys.exit(1)

    ws = _cdp_connect(tab)

    # 2. Navigate to read page
    read_url = READ_PAGE.format(bid=args.bid, sortnum=args.chapter)
    print(f"Opening: {read_url}", file=sys.stderr)
    _cdp_navigate(ws, read_url)

    # 3. Wait for content to load (SignalR inside browser handles this)
    print("Waiting for content...", file=sys.stderr)
    html = ''
    for i in range(15):
        time.sleep(2)
        length = int(_cdp_eval(ws, "document.querySelector('#q-app').innerText.length") or 0)
        online = '当前离线' not in (_cdp_eval(ws, "document.querySelector('#q-app').innerText") or '')
        print(f"  [{2*(i+1)}s] len={length}, online={online}", file=sys.stderr)
        if length > 2000 and online:
            html = _cdp_eval(ws, "document.querySelector('#q-app').innerHTML") or ''
            if len(html) > 5000:
                break

    if len(html) < 2000:
        print("ERROR: Content not loaded. Are you logged in?", file=sys.stderr)
        ws.close(); sys.exit(1)

    print(f"  HTML: {len(html)} chars", file=sys.stderr)

    # 4. Extract font URL from CSS
    font_src = _cdp_eval(ws, '''
    Array.from(document.styleSheets).flatMap(s => {
        try { return Array.from(s.cssRules||[]).filter(r =>
            r instanceof CSSFontFaceRule && r.style.fontFamily==="read"
        ).map(r => r.style.getPropertyValue("src")); }
        catch(e) { return []; }
    }).join("|")
    ''') or ''
    font_url = FONT_URL_RE.search(font_src)
    font_url = font_url.group(0) if font_url else None
    print(f"  Font URL: {font_url}", file=sys.stderr)

    # 5. Download font
    font_data = None
    if font_url:
        font_data = _get_font(ws, font_url)
    if not font_data:
        print("WARNING: Font not available, output will be garbled", file=sys.stderr)

    # 6. Decode
    if font_data:
        text, inv_n = _decode(html, font_data)
        print(f"  Decoded: {len(text)} chars ({inv_n} invisible removed)", file=sys.stderr)
    else:
        text = re.sub(r'<[^>]+>', '', html)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

    # 7. Output
    ws.close()
    if args.output:
        Path(args.output).write_text(text, encoding='utf-8')
        print(f"Saved: {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == '__main__':
    main()
