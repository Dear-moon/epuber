#!/usr/bin/env python3
"""
Render HTML (with embedded WOFF2 font) in Edge and OCR to text.

Strategy:
  1. CDP: one-shot full-page screenshot (reliable, no scrolling issues)
  2. PIL: slice tall screenshot into horizontal strips
  3. vision-reader: OCR each strip independently
  4. Combine text output

Usage:
  python html2txt.py chapter8.html -o chapter8.txt
"""

import sys, io, json, time, base64, re, argparse, subprocess
from pathlib import Path

if sys.platform == 'win32':
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except: pass

try:
    from PIL import Image
except ImportError:
    print("Error: pip install Pillow", file=sys.stderr); sys.exit(1)

try:
    import urllib.request
    import websocket as _ws
except ImportError:
    print("Error: pip install websocket-client", file=sys.stderr); sys.exit(1)

CDP = 'http://127.0.0.1:9222'
VISION_READER = str(Path(__file__).parent.parent.parent / 'vision-reader' / 'scripts' / 'read_image.py')

# ============================================================
#  CDP helpers (simple, resilient)
# ============================================================

def _cdp_get(path):
    with urllib.request.urlopen(f'{CDP}{path}', timeout=5) as r:
        return json.loads(r.read().decode())

def _find_file_tab():
    """Find a tab already showing our file, or any available page tab."""
    tabs = _cdp_get('/json')
    for t in tabs:
        if t['type'] == 'page' and t.get('url', '').startswith('file://'):
            return t
    for t in tabs:
        if t['type'] == 'page':
            return t
    return None

def _cdp_connect(tab):
    return _ws.create_connection(
        tab['webSocketDebuggerUrl'], timeout=30, suppress_origin=True,
        ping_interval=10, ping_timeout=5)

def _cdp_send(ws, method, params=None, timeout=30):
    mid = int(time.time() * 1000) % 100000
    msg = {'id': mid, 'method': method, 'params': params or {}}
    ws.send(json.dumps(msg))
    ws.settimeout(timeout)
    for _ in range(500):
        raw = ws.recv()
        resp = json.loads(raw)
        if resp.get('id') == mid:
            if 'error' in resp:
                raise RuntimeError(f"CDP error: {resp['error']}")
            return resp.get('result', {})
    raise TimeoutError(f"CDP timeout: {method}")

def _cdp_eval(ws, expr, timeout=12):
    result = _cdp_send(ws, 'Runtime.evaluate',
                       {'expression': expr, 'returnByValue': True}, timeout=timeout)
    return result.get('result', {}).get('value', '')

def _screenshot_fullpage(ws, output_path):
    """One-shot full-page screenshot. Reliable — works first time each connection."""
    result = _cdp_send(ws, 'Page.captureScreenshot', {
        'format': 'png',
        'captureBeyondViewport': True,
        'fromSurface': True,
    }, timeout=60)
    data_b64 = result.get('data', '')
    if data_b64:
        Path(output_path).write_bytes(base64.b64decode(data_b64))
        return len(data_b64)
    return 0


# ============================================================
#  Image slicing
# ============================================================

def _slice_image(image_path, output_dir, num_strips=5, overlap_ratio=0.1):
    """Slice a tall PNG into horizontal strips with overlap."""
    img = Image.open(image_path)
    w, h = img.size
    strip_h = h // num_strips
    overlap = int(strip_h * overlap_ratio)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    strips = []
    for i in range(num_strips):
        y1 = max(0, i * strip_h - (overlap if i > 0 else 0))
        y2 = min(h, (i + 1) * strip_h + (overlap if i < num_strips - 1 else 0))
        strip = img.crop((0, y1, w, y2))
        path = output_dir / f'strip_{i:02d}.png'
        strip.save(str(path), optimize=True)
        strips.append((str(path), y1, y2))
        print(f"  Strip {i}: y={y1}-{y2} ({y2-y1}px) -> {path}", file=sys.stderr)

    return strips


# ============================================================
#  OCR via vision-reader
# ============================================================

def _ocr_strip(image_path, strip_idx, output_dir):
    """OCR a single strip via vision-reader VLM."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f'ocr_{strip_idx:02d}.txt'

    if out_file.exists() and out_file.stat().st_size > 10:
        text = out_file.read_text(encoding='utf-8')
        return text.strip()

    print(f"  OCR strip {strip_idx}: sending to VLM...", file=sys.stderr)

    cmd = [
        sys.executable, VISION_READER,
        image_path,
        '--prompt',
        '请逐字识别并输出这张图片中的所有中文字符和标点符号。'
        '严格保持原文的段落结构和换行。'
        '不要添加任何解释、说明、标题或总结。'
        '只输出图片中实际显示的文字。'
        '这是小说正文截图的一段。',
        '--output', str(out_file),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(f"    Strip {strip_idx}: OCR timed out", file=sys.stderr)

    if out_file.exists():
        text = out_file.read_text(encoding='utf-8')
        # Strip the "结果已保存到" prefix if present
        if text.startswith('结果已保存到'):
            parts = text.split('\n', 1)
            text = parts[1] if len(parts) > 1 else text
        return text.strip()
    return ''


# ============================================================
#  Text post-processing
# ============================================================

def _deduplicate_text(segments):
    """Remove duplicate lines from overlapping segments."""
    lines = []
    for seg in segments:
        for line in seg.split('\n'):
            line = line.strip()
            if not line:
                if lines and lines[-1] != '':
                    lines.append('')
                continue
            # Skip if this line is contained in the last few lines (overlap dedup)
            if lines and len(lines) >= 2:
                # Check if this line appears in the last 3 non-empty lines
                recent = [l for l in lines[-4:] if l]
                if line in recent:
                    continue
            lines.append(line)
    return '\n'.join(lines)


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Render HTML in Edge → slice screenshot → OCR via VLM')
    parser.add_argument('html_file', help='Self-contained HTML file')
    parser.add_argument('-o', '--output', help='Output text file (auto-named if not given)')
    parser.add_argument('--strips', type=int, default=4,
                        help='Number of horizontal strips to slice into')
    parser.add_argument('--no-screenshot', action='store_true',
                        help='Skip rendering, use existing screenshot')
    args = parser.parse_args()

    html_path = Path(args.html_file).resolve()
    if not html_path.exists():
        print(f"ERROR: HTML file not found: {html_path}", file=sys.stderr)
        sys.exit(1)

    screenshot_path = html_path.with_suffix('.png')
    output_dir = html_path.parent / '_ocr_strips'

    # === Phase 1: Render & screenshot ===
    if not args.no_screenshot:
        print("Connecting to Edge CDP...", file=sys.stderr)
        tab = _find_file_tab()
        if not tab:
            print("ERROR: No Edge tab. Start Edge with:", file=sys.stderr)
            print(r'  msedge --remote-debugging-port=9222 --remote-allow-origins=*', file=sys.stderr)
            sys.exit(1)
        print(f"  Tab: {tab.get('url', '?')[:80]}", file=sys.stderr)

        ws = _cdp_connect(tab)

        # Set viewport for consistent rendering
        _cdp_send(ws, 'Emulation.setDeviceMetricsOverride', {
            'width': 900, 'height': 800,
            'deviceScaleFactor': 1,
            'mobile': False,
        })

        # Navigate to file
        file_url = html_path.as_uri()
        print(f"Loading: {file_url}", file=sys.stderr)
        _cdp_send(ws, 'Page.navigate', {'url': file_url}, timeout=15)

        # Wait for render
        print("Waiting for font + content...", file=sys.stderr)
        time.sleep(3)
        for i in range(12):
            time.sleep(1)
            try:
                tl = int(_cdp_eval(ws, "document.body.innerText.length") or 0)
                fs = _cdp_eval(ws, "document.fonts.status") or ''
                print(f"  [{i+1}s] text={tl}, fonts={fs}", file=sys.stderr)
                if tl > 500 and fs == 'loaded':
                    break
            except Exception:
                print(f"  [{i+1}s] CDP error, retrying...", file=sys.stderr)
                time.sleep(2)
        time.sleep(2)

        # Screenshot
        print(f"Capturing full-page screenshot...", file=sys.stderr)
        size = _screenshot_fullpage(ws, str(screenshot_path))
        ws.close()
        print(f"  {size:,} base64 chars -> {screenshot_path}", file=sys.stderr)

        if size < 100:
            print("ERROR: Screenshot failed", file=sys.stderr)
            sys.exit(1)

    # === Phase 2: Slice ===
    print(f"\nSlicing into {args.strips} horizontal strips...", file=sys.stderr)
    strips_dir = output_dir / 'strips'
    strips = _slice_image(str(screenshot_path), strips_dir, args.strips)

    # === Phase 3: OCR each strip ===
    print(f"\nOCR with vision-reader ({args.strips} strips)...", file=sys.stderr)
    texts = []
    for i, (path, y1, y2) in enumerate(strips):
        text = _ocr_strip(path, i, output_dir)
        if text:
            texts.append(text)
            print(f"  Strip {i}: {len(text)} chars extracted", file=sys.stderr)
        else:
            print(f"  Strip {i}: no text extracted", file=sys.stderr)

    # === Phase 4: Combine ===
    full_text = _deduplicate_text(texts)
    print(f"\nTotal: {len(full_text)} chars", file=sys.stderr)

    if args.output:
        out_path = Path(args.output)
    else:
        # Auto-name: replace .html with _ocr.txt in same directory
        out_path = html_path.with_suffix('').with_suffix('')
        out_path = Path(str(out_path) + '_ocr.txt')
    out_path.write_text(full_text, encoding='utf-8')
    print(f"Saved: {out_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
