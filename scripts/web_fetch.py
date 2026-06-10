#!/usr/bin/env python3
"""
Web novel fetcher using novelia.cc API (supports hameln and other sources).
Also attempts direct fetch for syosetu.org (may be blocked by Cloudflare).

API endpoints:
  Novel metadata: GET /api/novel/{source}/{novel_id}
  Chapter content: GET /api/novel/{source}/{novel_id}/chapter/{chapter_id}
"""

import re
import sys
import io
import json
import time
import argparse
from pathlib import Path

# Fix Windows terminal encoding for CJK output
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    import requests
except ImportError:
    print("Error: 'requests' library required. Install: pip install requests", file=sys.stderr)
    sys.exit(1)

# ============================================================
#  API client
# ============================================================

NOVELIA_API = 'https://n.novelia.cc/api'

# Translation sources available on novelia.cc
TRANSLATIONS = {
    'jp': 'paragraphs',           # Original Japanese
    'youdao': 'youdaoParagraphs',  # Youdao machine translation
    'gpt': 'gptParagraphs',       # GPT machine translation
    'sakura': 'sakuraParagraphs',  # Sakura LLM translation (best quality)
}

VALID_SOURCES = ['hameln', 'syosetu', 'narou', 'kakuyomu', 'novelup', 'alphapolis']


def create_session():
    """Create a requests session with browser-mimicking headers."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'zh-CN,zh;q=0.9,ja;q=0.8',
    })
    return session


def parse_url(url):
    """Parse a novelia.cc or syosetu.org URL into (source, novel_id).

    Examples:
      https://n.novelia.cc/novel/hameln/68239 → ('hameln', '68239')
      https://syosetu.org/novel/68239/      → ('syosetu', '68239')
    """
    # novelia.cc: /novel/{source}/{id}
    m = re.search(r'novelia\.cc/novel/(\w+)/(\d+)', url)
    if m:
        return m.group(1), m.group(2)

    # syosetu.org: /novel/{id}/
    m = re.search(r'syosetu\.org/novel/(\d+)', url)
    if m:
        return 'syosetu', m.group(1)

    raise ValueError(f"Cannot parse URL: {url}. Expected novelia.cc or syosetu.org format.")


def fetch_metadata(source, novel_id, session):
    """Fetch novel metadata from novelia.cc API."""
    url = f'{NOVELIA_API}/novel/{source}/{novel_id}'
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"API returned {resp.status_code} for {url}")
    data = resp.json()

    return {
        'title_jp': data.get('titleJp', ''),
        'title_zh': data.get('titleZh', ''),
        'title': data.get('titleZh', '') or data.get('titleJp', 'Untitled'),
        'authors': [a['name'] for a in data.get('authors', [])],
        'author': data['authors'][0]['name'] if data.get('authors') else 'Unknown',
        'type': data.get('type', ''),
        'keywords': data.get('keywords', []),
        'total_chars': data.get('totalCharacters', 0),
        'introduction_jp': data.get('introductionJp', ''),
        'introduction_zh': data.get('introductionZh', ''),
        'toc': data.get('toc', []),
    }


def fetch_chapter(source, novel_id, chapter_id, translation, session):
    """Fetch a single chapter's content from novelia.cc API.

    Args:
        translation: one of 'jp', 'youdao', 'gpt', 'sakura'

    Returns:
        dict with keys: title_jp, title_zh, paragraphs, next_id
    """
    url = f'{NOVELIA_API}/novel/{source}/{novel_id}/chapter/{chapter_id}'
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Chapter API returned {resp.status_code} for {url}")
    data = resp.json()

    para_key = TRANSLATIONS.get(translation, TRANSLATIONS['sakura'])
    paragraphs = data.get(para_key, data.get('paragraphs', []))

    return {
        'title_jp': data.get('titleJp', ''),
        'title_zh': data.get('titleZh', ''),
        'paragraphs': paragraphs,
        'next_id': data.get('nextId', ''),
    }


# ============================================================
#  Main fetcher
# ============================================================

def fetch_novel(url, translation='sakura', delay=0.3):
    """Fetch complete novel from a supported web source.

    Args:
        url: Novel index URL (novelia.cc or syosetu.org)
        translation: which translation to fetch ('jp', 'youdao', 'gpt', 'sakura')
        delay: seconds to wait between chapter requests (be polite)

    Returns:
        (combined_text, metadata_dict)
    """
    session = create_session()
    source, novel_id = parse_url(url)

    # Fetch metadata
    print(f"Fetching metadata for {source}/{novel_id}...")
    try:
        meta = fetch_metadata(source, novel_id, session)
    except RuntimeError:
        # Try alternate source mapping for syosetu.org
        if source == 'syosetu':
            print("  Direct syosetu fetch failed. Trying alternate sources...")
            for alt_source in ['narou', 'hameln']:
                try:
                    meta = fetch_metadata(alt_source, novel_id, session)
                    source = alt_source
                    print(f"  Found on {source}!")
                    break
                except RuntimeError:
                    continue
            else:
                raise RuntimeError(
                    f"Cannot fetch novel {novel_id} from novelia.cc.\n"
                    f"The syosetu.org site is Cloudflare-protected and cannot be scraped directly.\n"
                    f"Try downloading the TXT manually and using convert.py instead."
                )
        else:
            raise

    print(f"  Title: {meta['title']}")
    print(f"  Author: {meta['author']}")
    print(f"  Source: {source}")

    toc = meta['toc']
    if not toc:
        raise RuntimeError("No chapters found in table of contents")
    print(f"  Chapters: {len(toc)}")
    print(f"  Translation: {translation} ({TRANSLATIONS[translation]})")

    # Build combined text
    parts = []
    # Add metadata header
    parts.append(f"# {meta['title']}")
    parts.append(f"作者: {meta['author']}")
    if meta.get('introduction_zh'):
        parts.append(f"\n简介: {meta['introduction_zh']}")
    parts.append('')

    # Fetch each chapter
    ch_count = 0
    for i, ch in enumerate(toc):
        # TOC items without chapterId are section headers
        if 'chapterId' not in ch:
            section_title = ch.get('titleZh', ch.get('titleJp', ''))
            print(f"  [{i+1}/{len(toc)}] [{section_title}] (section header)")
            parts.append(f'\n\n# {section_title}\n')
            continue

        ch_id = ch['chapterId']
        ch_title_zh = ch.get('titleZh', ch.get('titleJp', f'Chapter {ch_id}'))
        ch_count += 1

        print(f"  [{i+1}/{len(toc)}] {ch_title_zh[:50]}...", end=' ', flush=True)
        try:
            ch_data = fetch_chapter(source, novel_id, ch_id, translation, session)
            # Build chapter text
            parts.append(f'\n\n第{ch_count}章 {ch_title_zh}\n')
            for para in ch_data['paragraphs']:
                para = para.strip()
                if para:
                    parts.append(para)
                else:
                    parts.append('')  # preserve blank lines within chapters
            print('OK')
        except Exception as e:
            print(f'FAILED: {e}')
            parts.append(f'\n\n第{ch_count}章 {ch_title_zh}\n')
            parts.append(f'[获取失败: {e}]')

        # Be polite to the server
        if delay > 0:
            time.sleep(delay)

    combined = '\n'.join(parts)
    return combined, meta


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Fetch web novel and prepare for EPUB conversion',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python web_fetch.py https://n.novelia.cc/novel/hameln/68239
  python web_fetch.py https://n.novelia.cc/novel/hameln/68239 -t gpt -o novel.txt
  python web_fetch.py https://syosetu.org/novel/68239/ -o novel.txt
        ''')
    parser.add_argument('url', help='Novel index page URL (novelia.cc or syosetu.org)')
    parser.add_argument('-o', '--output', help='Output TXT file path')
    parser.add_argument('-t', '--translation', default='sakura',
                        choices=['jp', 'youdao', 'gpt', 'sakura'],
                        help='Translation source (default: sakura)')
    parser.add_argument('-d', '--delay', type=float, default=0.3,
                        help='Delay between chapter requests in seconds (default: 0.3)')
    args = parser.parse_args()

    print(f"Fetching: {args.url}")
    text, meta = fetch_novel(args.url, translation=args.translation, delay=args.delay)

    print(f"\nTitle: {meta['title']}")
    print(f"Author: {meta['author']}")
    print(f"Total length: {len(text):,} chars")

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"Saved to: {args.output}")
    else:
        # Default output filename
        safe_title = re.sub(r'[^\w一-鿿]', '_', meta['title']).strip('_') or 'novel'
        out_path = f'{safe_title}.txt'
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"Saved to: {out_path}")

    return text, meta


if __name__ == '__main__':
    main()
