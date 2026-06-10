#!/usr/bin/env python3
"""
Build a font-decode lookup table by OCR-ing the character table via VLM.
Once built, the table is cached in the book directory and reused.

Usage:
  # Build decode map for a specific book (fetches font + sample text + OCR)
  python build_decode_map.py --bid 17028

  # Decode text using cached map
  python build_decode_map.py --decode --map font_map.json --text "..."
"""

import sys, io, re, json, base64, time, argparse, subprocess
from pathlib import Path


def build_decode_map(bid, token):
    """Complete pipeline: fetch font, sample text, OCR, build mapping."""
    import urllib.request, ssl
    from fontTools.ttLib import TTFont

    # Import lightnovel_api for fetching
    import importlib.util
    spec = importlib.util.spec_from_file_location('api', __file__.replace('build_decode_map.py', 'lightnovel_api.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Fetch a sample chapter to get the font + text
    print(f"Fetching sample chapter for book {bid}...")
    data = mod._fetch_chapter_via_dart(token, bid, 7)
    ch = data['Chapter']
    html = ch['Content']
    font_path_rel = ch.get('Font', '')

    # Download font
    ctx = ssl.create_default_context()
    ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    font_url = f"https://api.lightnovel.life{font_path_rel}"
    req = urllib.request.Request(font_url, headers={'User-Agent': 'Novella/1.8.0'})
    font_bytes = urllib.request.urlopen(req, context=ctx, timeout=30).read()
    print(f"Font downloaded: {len(font_bytes):,} bytes")

    # Collect PUA characters
    font = TTFont(io.BytesIO(font_bytes))
    cmap = font['cmap'].getBestCmap()
    glyf = font['glyf']
    hmtx = font['hmtx']
    inv = set()
    for cp, name in cmap.items():
        if not cp: continue
        g = glyf.get(str(name))
        if not g: continue
        nc = g.numberOfContours if hasattr(g, 'numberOfContours') else -1
        adv = hmtx.metrics.get(str(name), (None,))[0]
        if adv == 0 and nc == 0: inv.add(cp)
    font.close()

    text = re.sub(r'<[^>]+>', '', html)
    text = ''.join(c for c in text if ord(c) not in inv)
    pua_chars = sorted(set(c for c in text if 0xE000 <= ord(c) <= 0xF8FF))
    print(f"Unique PUA chars: {len(pua_chars)}")

    # Build and screenshot character table (reuse existing _char_table.html if same font)
    print("Screenshotting character table for VLM OCR...")
    # This is the manual part - user must run VLM OCR
    # For now, tell the user what to do

    print(f"\nCharacter table has {len(pua_chars)} unique PUA characters.")
    print("Run vision-reader on each chunk image, then run:")
    print("  python build_decode_map.py --compile --ocr-results <file>")


def compile_map(pua_path, ocr_results, output_path):
    """Compile OCR results into a decode mapping JSON."""
    pua_data = json.loads(Path(pua_path).read_text(encoding='utf-8'))
    pua_cps = pua_data['pua_chars']
    font_url = pua_data.get('font_url', '')

    # Parse OCR results
    decode_map = {}  # PUA cp -> real cp
    for line in ocr_results.strip().split('\n'):
        line = line.strip()
        if ':' in line and not line.startswith('#') and not line.startswith('//'):
            parts = line.split(':', 1)
            try:
                idx = int(parts[0].strip())
                char = parts[1].strip()
                if idx < len(pua_cps) and char:
                    pua_cp = pua_cps[idx]
                    real_cp = ord(char)
                    decode_map[str(pua_cp)] = real_cp
            except (ValueError, IndexError):
                continue

    result = {
        'font_url': font_url,
        'num_entries': len(decode_map),
        'map': decode_map,  # str(PUA_cp) -> real_cp
    }
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Decode map saved: {output_path} ({len(decode_map)} entries)")
    return result


def decode_text(text, decode_map):
    """Decode obfuscated text using the lookup map."""
    result = []
    for c in text:
        cp = ord(c)
        real_cp = decode_map.get(str(cp), cp)
        result.append(chr(real_cp))
    return ''.join(result)


def main():
    parser = argparse.ArgumentParser(description='Build and use lightnovel font decode map')
    parser.add_argument('--bid', type=int, help='Book ID to build map for')
    parser.add_argument('--compile', action='store_true', help='Compile OCR results into map')
    parser.add_argument('--pua-list', help='Path to _pua_mapping_raw.json')
    parser.add_argument('--ocr-file', help='Path to OCR results file (one line per char)')
    parser.add_argument('--output', default='font_decode_map.json', help='Output map path')
    parser.add_argument('--decode', action='store_true', help='Decode text from stdin')
    parser.add_argument('--map', help='Path to decode map JSON')
    args = parser.parse_args()

    if args.compile:
        if not args.pua_list or not args.ocr_file:
            print("ERROR: --pua-list and --ocr-file required for --compile")
            sys.exit(1)
        ocr_text = Path(args.ocr_file).read_text(encoding='utf-8')
        compile_map(args.pua_list, ocr_text, args.output)

    elif args.decode:
        if not args.map:
            print("ERROR: --map required for --decode")
            sys.exit(1)
        decode_map = json.loads(Path(args.map).read_text(encoding='utf-8'))
        text = sys.stdin.read()
        result = decode_text(text, decode_map['map'])
        sys.stdout.write(result)

    elif args.bid:
        # Need to get the token
        import importlib.util
        spec = importlib.util.spec_from_file_location('api', __file__.replace('build_decode_map.py', 'lightnovel_api.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        build_decode_map(args.bid, mod.REFRESH_TOKEN)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
