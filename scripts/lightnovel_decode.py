#!/usr/bin/env python3
"""
Lightnovel.app font decoder + automatic font download.
One-stop: download chapter font + decode HTML.

Usage:
  # Decode a captured HTML file (font auto-downloaded)
  python lightnovel_decode.py chapter_8_snapshot.txt -o output.txt

  # Decode raw HTML text from stdin
  python lightnovel_decode.py --html "<div>garbled text</div>" --font-url "https://api.lightnovel.life/font/xxx.woff2"

  # Decode with existing font file
  python lightnovel_decode.py --html-file page.html --font-file font.woff2
"""

import sys, io, re, argparse, base64, requests, urllib3
from pathlib import Path

urllib3.disable_warnings()

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except: pass

try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: pip install fonttools brotli", file=sys.stderr)
    sys.exit(1)


def download_font(font_url, dest=None):
    """Download a WOFF2 font from api.lightnovel.life (verify=False required)."""
    if not dest:
        dest = f"_font_{abs(hash(font_url))}.woff2"
    print(f"Downloading font: {font_url}", file=sys.stderr)
    r = requests.get(font_url, verify=False, timeout=30)
    r.raise_for_status()
    Path(dest).write_bytes(r.content)
    print(f"  Saved: {dest} ({len(r.content):,} bytes)", file=sys.stderr)
    return dest


def extract_invisible_set(font_path):
    """Extract codepoints that render as invisible (advance=0, 0 contours)."""
    font = TTFont(font_path)
    cmap = font["cmap"].getBestCmap()
    glyf = font["glyf"]
    hmtx = font["hmtx"]
    invisible = set()
    for cp, name in cmap.items():
        if not cp: continue
        g = glyf.get(name)
        if not g: continue
        nc = g.numberOfContours if hasattr(g, 'numberOfContours') else -1
        adv = hmtx.metrics.get(name, (None,))[0]
        if adv == 0 and nc == 0:
            invisible.add(cp)
    return invisible


def clean_text(text, invisible_set):
    """Remove invisible codepoints from text."""
    return ''.join(c for c in text if ord(c) not in invisible_set)


def strip_html(html):
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r'<[^>]+>', '', html)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def decode(html, font_path, keep_html=False):
    """Decode font-obfuscated HTML. Returns clean text."""
    inv = extract_invisible_set(font_path)
    cleaned = clean_text(html, inv)
    if keep_html:
        return cleaned, len(inv)
    return strip_html(cleaned), len(inv)


def extract_font_url_from_file(filepath):
    """Extract font URL from a snapshot file (FONT_URL:xxx format)."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    m = re.search(r'FONT_URL:(https://api\.lightnovel\.life/font/[a-f0-9]+\.woff2)', content)
    return m.group(1) if m else None


def extract_html_from_file(filepath):
    """Extract HTML from a snapshot file (===HTML=== marker)."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    idx = content.find("===HTML===")
    if idx > 0:
        return content[idx + 11:]
    return content


def main():
    parser = argparse.ArgumentParser(description='Lightnovel.app font decoder')
    parser.add_argument('input', nargs='?', help='Snapshot file or HTML file to decode')
    parser.add_argument('-o', '--output', help='Output file (stdout if omitted)')
    parser.add_argument('--font-url', help='Font URL to download')
    parser.add_argument('--font-file', help='Local font file')
    parser.add_argument('--html', help='Raw HTML string to decode')
    parser.add_argument('--html-file', help='HTML file to decode')
    parser.add_argument('--keep-html', action='store_true', help='Keep HTML tags in output')
    args = parser.parse_args()

    # Determine input source
    html = None
    font_url = args.font_url
    font_file = args.font_file

    if args.html:
        html = args.html
    elif args.html_file:
        with open(args.html_file, 'r', encoding='utf-8') as f:
            html = f.read()
    elif args.input:
        # Snapshot file format: FONT_URL:xxx\n===HTML===\n<html>...
        extracted_url = extract_font_url_from_file(args.input)
        if extracted_url:
            font_url = font_url or extracted_url
        if "===HTML===" in open(args.input, 'r', encoding='utf-8').read():
            html = extract_html_from_file(args.input)
        else:
            with open(args.input, 'r', encoding='utf-8') as f:
                html = f.read()
    else:
        # Read from stdin
        html = sys.stdin.read()

    if not html:
        parser.error("No HTML input. Use --html, --html-file, input file, or stdin.")

    # Get font
    if not font_file and font_url:
        font_file = download_font(font_url)
    elif not font_file:
        parser.error("Font source required: --font-url, --font-file, or snapshot file with FONT_URL")

    # Decode
    result, inv_count = decode(html, font_file, keep_html=args.keep_html)
    print(f"Decoded: {len(html)} -> {len(result)} chars ({inv_count} invisible)", file=sys.stderr)

    # Output
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(result)
        print(f"Saved: {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(result)


if __name__ == '__main__':
    main()
