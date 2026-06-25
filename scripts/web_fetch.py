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
from concurrent.futures import ThreadPoolExecutor, as_completed

# Fix Windows terminal encoding for CJK output
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"


def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_novel(source, novel_id, title, chapter_count):
    """Record novel fetch in memory."""
    try:
        data = _load_memory()
        key = f'novelia:{source}:{novel_id}'
        data[key] = {
            'title': title,
            'source': source,
            'chapter_count': chapter_count,
            'first_fetch': data.get(key, {}).get('first_fetch', time.strftime('%Y-%m-%d %H:%M')),
            'last_fetch': time.strftime('%Y-%m-%d %H:%M'),
        }
        _save_memory(data)
    except Exception:
        pass  # memory is non-critical

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
    """Parse a novel URL into (source, novel_id) for the novelia.cc API.

    Supported formats:
      https://n.novelia.cc/novel/hameln/<ID>   → ('hameln', '<ID>')
      https://novelia.cc/novel/syosetu/<ID>   → ('syosetu', '<ID>')
      https://syosetu.org/novel/<ID>/          → ('syosetu', '<ID>')
      https://novel18.syosetu.com/<ID>/        → ('narou', '<ID>')
      https://ncode.syosetu.com/<ID>/          → ('narou', '<ID>')
    """
    # novelia.cc: /novel/{source}/{id}  (id may be numeric or alphanumeric)
    m = re.search(r'novelia\.cc/novel/(\w+)/([\w\d]+)', url)
    if m:
        return m.group(1), m.group(2)

    # syosetu.org: /novel/{id}/
    m = re.search(r'syosetu\.org/novel/(\d+)', url)
    if m:
        return 'syosetu', m.group(1)

    # syosetu.com (narou, including novel18/ncode subdomains): /{id}/
    m = re.search(r'syosetu\.com/(n\w+?)(?:/|\b)', url)
    if m:
        # Try 'narou' source first, fall back to 'syosetu'
        return 'narou', m.group(1)

    raise ValueError(f"Cannot parse URL: {url}. Expected novelia.cc, syosetu.org, or syosetu.com format.")


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

def _fetch_one_chapter(source, novel_id, ch_id, translation, delay, ch_title_zh):
    """Fetch a single chapter in its own session (thread-safe)."""
    session = create_session()
    try:
        ch_data = fetch_chapter(source, novel_id, ch_id, translation, session)
        return ch_data, None
    except Exception as e:
        return None, str(e)
    finally:
        if delay > 0:
            time.sleep(delay)


def _fetch_batch(chapter_items, source, novel_id, translation, delay, workers, label=""):
    """Fetch a batch of chapters, returning {toc_index: (ch_data, error)}."""
    results = {}
    prefix = f"  [{label}] " if label else "  "

    if workers > 1 and len(chapter_items) > 1:
        print(f"{prefix}Workers: {workers}, chapters: {len(chapter_items)}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for idx, ch in chapter_items:
                ch_id = ch['chapterId']
                ch_title = ch.get('titleZh', ch.get('titleJp', f'Chapter {ch_id}'))
                f = executor.submit(_fetch_one_chapter, source, novel_id,
                                   ch_id, translation, delay, ch_title)
                futures[f] = (idx, ch_title)

            done = 0
            for f in as_completed(futures):
                idx, ch_title = futures[f]
                ch_data, error = f.result()
                results[idx] = (ch_data, error)
                done += 1
                status = 'OK' if not error else f'FAILED'
                print(f"  [{done}/{len(chapter_items)}] {ch_title[:50]}... {status}", flush=True)
    else:
        for idx, ch in chapter_items:
            ch_id = ch['chapterId']
            ch_title = ch.get('titleZh', ch.get('titleJp', f'Chapter {ch_id}'))
            ch_data, error = _fetch_one_chapter(source, novel_id, ch_id, translation, delay, ch_title)
            results[idx] = (ch_data, error)
            status = 'OK' if not error else f'FAILED'
            print(f"  [{len(results)}/{len(chapter_items)}] {ch_title[:50]}... {status}", flush=True)

    return results


def fetch_novel(url, translation='sakura', delay=0.3, workers=5):
    """Fetch complete novel from a supported web source.

    Args:
        url: Novel index URL (novelia.cc or syosetu.org)
        translation: which translation to fetch ('jp', 'youdao', 'gpt', 'sakura')
        delay: seconds to wait between requests per worker (be polite)
        workers: concurrent download threads (1 = sequential)

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
        if source in ('syosetu', 'narou'):
            print(f"  Direct {source} fetch failed. Trying alternate sources...")
            # Mutual fallback: narou ↔ syosetu, plus hameln for hameln-sourced syosetu URLs
            fallbacks = ['narou', 'syosetu', 'hameln']
            fallbacks = [s for s in fallbacks if s != source]  # don't retry same source
            for alt_source in fallbacks:
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

    # Separate TOC items needing API calls from section headers
    chapter_items = []   # [(toc_index, ch)]
    for i, ch in enumerate(toc):
        if 'chapterId' in ch:
            chapter_items.append((i, ch))

    # First pass: parallel fetch
    results = _fetch_batch(chapter_items, source, novel_id, translation, delay, workers)

    # Retry loop: retry failed chapters up to 3 times with increasing backoff
    retry_delays = [1.0, 2.0, 4.0]
    for retry_round, retry_delay in enumerate(retry_delays, 1):
        failed = [(idx, ch) for idx, ch in chapter_items
                  if idx in results and results[idx][1] is not None]
        if not failed:
            break

        print(f"\n  Retry round {retry_round}: {len(failed)} failed chapters "
              f"(delay={retry_delay}s, sequential)...", flush=True)
        retry_results = _fetch_batch(failed, source, novel_id, translation,
                                     retry_delay, workers=1, label=f"retry{retry_round}")
        results.update(retry_results)

    # Final failure count
    final_failed = sum(1 for v in results.values() if v[1] is not None)
    if final_failed > 0:
        print(f"\n  {final_failed} chapter(s) failed after all retries.")
    else:
        print(f"\n  All chapters fetched successfully.")

    # Build combined text in TOC order
    parts = []
    parts.append(f"# {meta['title']}")
    parts.append(f"作者: {meta['author']}")
    if meta.get('introduction_zh'):
        parts.append(f"\n简介: {meta['introduction_zh']}")
    parts.append('')

    ch_count = 0
    for i, ch in enumerate(toc):
        if 'chapterId' not in ch:
            section_title = ch.get('titleZh', ch.get('titleJp', ''))
            parts.append(f'\n\n# {section_title}\n')
            continue

        ch_id = ch['chapterId']
        ch_title_zh = ch.get('titleZh', ch.get('titleJp', f'Chapter {ch_id}'))
        ch_count += 1

        if i in results:
            ch_data, error = results[i]
            if ch_data and not error:
                parts.append(f'\n\n第{ch_count}章 {ch_title_zh}\n')
                for para in ch_data['paragraphs']:
                    para = para.strip()
                    if para:
                        parts.append(para)
                    else:
                        parts.append('')
            else:
                parts.append(f'\n\n第{ch_count}章 {ch_title_zh}\n')
                parts.append(f'[获取失败: {error}]')
        else:
            parts.append(f'\n\n第{ch_count}章 {ch_title_zh}\n[获取失败: 未知错误]')

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
  python web_fetch.py https://n.novelia.cc/novel/hameln/<ID>
  python web_fetch.py https://n.novelia.cc/novel/hameln/<ID> -t gpt -o novel.txt -w 20
  python web_fetch.py https://syosetu.org/novel/<ID>/ -o novel.txt
  python web_fetch.py https://novel18.syosetu.com/<ID>/ -o novel.txt
        ''')
    parser.add_argument('url', help='Novel index page URL (novelia.cc or syosetu.org)')
    parser.add_argument('-o', '--output', help='Output TXT file path')
    parser.add_argument('-t', '--translation', default='sakura',
                        choices=['jp', 'youdao', 'gpt', 'sakura'],
                        help='Translation source (default: sakura)')
    parser.add_argument('-d', '--delay', type=float, default=0.3,
                        help='Delay between chapter requests in seconds (default: 0.3)')
    parser.add_argument('-w', '--workers', type=int, default=5,
                        help='Concurrent download threads (default: 5, 1 = sequential)')
    args = parser.parse_args()

    print(f"Fetching: {args.url}")
    text, meta = fetch_novel(args.url, translation=args.translation,
                            delay=args.delay, workers=args.workers)

    # Record in fetch memory
    src, nid = parse_url(args.url)
    ch_count = len([c for c in meta.get('toc', []) if 'chapterId' in c])
    _mark_novel(src, nid, meta['title'], ch_count)

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
