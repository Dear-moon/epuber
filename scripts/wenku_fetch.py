#!/usr/bin/env python3
"""
novelia 文库版（wenku）下载器。

文库版是出版社正式发行的电子书（如角川スニーカー文庫），按卷分册，
内容与 web 版不同（含序章/终章/后记/特典）。本脚本调用 novelia 的
wenku API 下载完整卷册 EPUB，无需登录。

API 端点（已逆向验证）:
  元数据: GET /api/wenku/{wid}
  下载:   GET /api/wenku/{wid}/file/{quote(volumeId)}?mode=..&translationsMode=..&translations=..
          → 完整 EPUB (application/epub+zip, 无 DRM)

用法:
  python wenku_fetch.py "https://n.novelia.cc/wenku/<WID>"            # 下载全部卷
  python wenku_fetch.py "https://n.novelia.cc/wenku/<WID>" --list     # 只列卷
  python wenku_fetch.py "https://n.novelia.cc/wenku/<WID>" --volume 2 # 只下第2卷
"""

import re
import sys
import io
import json
import time
import argparse
import unicodedata
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# --- Config & memory (same pattern as web_fetch.py) ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from config import get_fetch_dir
except ImportError:
    def get_fetch_dir():
        return Path.cwd() / "fetch"

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"


def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_wenku(wid, title, volume_count):
    """Record wenku fetch in memory."""
    try:
        data = _load_memory()
        key = f'wenku:{wid}'
        data[key] = {
            'title': title,
            'source': 'wenku',
            'kind': 'wenku',
            'volume_count': volume_count,
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


NOVELIA_API = 'https://n.novelia.cc/api'

# mode: zh=Chinese only / zh-jp=Chinese-primary+JP / jp-zh=JP-primary+Chinese
# Note: pure-JP mode=jp is unsupported by the file endpoint (404/400), so omitted.
MODES = ['zh', 'zh-jp', 'jp-zh']
TRANSLATIONS = ['sakura', 'gpt', 'youdao']


def create_session():
    """Browser-mimicking session (same UA family as web_fetch.py)."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'zh-CN,zh;q=0.9,ja;q=0.8',
    })
    return session


def parse_wenku_url(url):
    """Extract wenku id from a novelia wenku URL.

    Supported:
      https://n.novelia.cc/wenku/<24-hex-id>
      https://novelia.cc/wenku/<24-hex-id>
    Returns the 24-hex wid string.
    """
    m = re.search(r'novelia\.cc/wenku/([0-9a-fA-F]{24})', url)
    if m:
        return m.group(1).lower()
    raise ValueError(
        f"Cannot parse wenku URL: {url}. Expected format: "
        "https://n.novelia.cc/wenku/<WID>"
    )


def fetch_metadata(wid, session):
    """Fetch wenku novel metadata (with retry for flaky connections)."""
    url = f'{NOVELIA_API}/wenku/{wid}'
    data = None
    for attempt in range(4):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(f"Wenku API returned {resp.status_code} for {url}")
            data = resp.json()
            if not isinstance(data, dict) or 'titleZh' not in data:
                # Unexpected structure; retry once
                if attempt == 3:
                    raise RuntimeError(f"Wenku metadata returned unexpected structure for {url}")
                time.sleep(1.0)
                continue
            break
        except requests.exceptions.ConnectionError:
            if attempt == 3:
                raise
            time.sleep(1.5)

    return {
        'wid': wid,
        'title': data.get('title', ''),
        'title_zh': data.get('titleZh', '') or data.get('title', 'Untitled'),
        'cover': data.get('cover', ''),
        'authors': data.get('authors', []),
        'artists': data.get('artists', []),
        'keywords': data.get('keywords', []),
        'publisher': data.get('publisher', ''),
        'imprint': data.get('imprint', ''),
        'level': data.get('level', ''),
        'introduction': data.get('introduction', ''),
        'web_ids': data.get('webIds', []),
        # Volume list: volumeJp first (always downloadable), volumeZh as fallback
        'volume_list': data.get('volumeJp', []) or data.get('volumeZh', []),
        'volumes_meta': data.get('volumes', []),  # asin/title/publishAt/cover
    }


def _build_volumes(meta):
    """Pair volume_list ↔ volumes_meta by title/number, sort by publish date.

    volumeJp entries look like:
      {'volumeId': '<book title>.epub', 'total': 26, 'sakura': 26, ...}
    volumes_meta entries look like:
      {'asin': 'B0...', 'title': '<book title>', 'publishAt': 1701360000, ...}

    标题格式在不同卷间会变体（'世界最強07' vs '世界最強 7'、'小篇集'、'12-BOOK特典' 等），
    匹配策略：
      1) 精确全文（NFKC 归一化）匹配
      2) 卷号匹配：标题中的数字若紧跟空白或结尾，视为卷号，按 (主标题, 卷号) 配对
         （数字后紧贴非空白后缀的如 '12-BOOK...' 特典不算卷号 → 保持未配对）
    配对成功的按 publishAt 排序；未配对的（特典等）追加在后。
    Returns list of dicts: {index, title, volume_id, publish_at, asin, counts}
    """
    def _norm(s):
        # NFKC normalise (full/half-width, kana) + collapse whitespace/separators
        s2 = unicodedata.normalize('NFKC', s or '').lower()
        s2 = re.sub(r'\[[^\]]*\]', '', s2)                      # [作者] 前缀
        s2 = re.sub(r'^[^一-龥ぁ-んァ-ヶa-z0-9]+', '', s2)      # 行首非内容前导
        s2 = re.sub(r'[+　　　 /]+', ' ', s2)                     # 各种分隔符折叠
        s2 = re.sub(r'[・・]', ' ', s2)
        s2 = re.sub(r'\s+', ' ', s2).strip()
        return s2

    def _num_key(s):
        """→ (主标题, 卷号|None)。识别 '第N巻'、独立/结尾数字为卷号。

        无卷号时主标题保留整串（供无卷号元数据匹配有卷号 volumeId 的兜底用）。
        """
        s2 = _norm(s)
        m = re.search(r'第(\d+)巻', s2)      # "第08巻"
        if m:
            return s2.replace(f'第{m.group(1)}巻', '').strip(), int(m.group(1))
        m = re.search(r'([0-9]+)(\s|$)', s2)  # 独立/结尾数字
        if m:
            return s2[:m.start(1)].rstrip(), int(m.group(1))
        return s2, None

    def _series(s):
        """去掉卷号后的全集系列名（如 'アサシンズプライド'、'Secret Garden'）。"""
        base, num = _num_key(s)
        # If the series has a number, the subtitle follows it; main title ends at _num_key
        return base

    # Order by volumeJp (volume_list); look up publish_at/asin per volume in volumes_meta
    vol_by_exact = {}
    vol_by_num = {}
    for v in meta['volume_list']:
        vid = v.get('volumeId', '')
        base = vid[:-5] if vid.endswith('.epub') else vid
        vol_by_exact.setdefault(_norm(base), v)
        key = _num_key(base)
        if key[1] is not None:
            vol_by_num.setdefault(key, v)

    # volumes_meta lookup: exact -> number -> series with no volume no.
    meta_by_exact = {}
    meta_by_num = {}
    meta_by_series = {}
    for vm in meta['volumes_meta']:
        title = (vm.get('title') or '').strip()
        meta_by_exact.setdefault(_norm(title), vm)
        key = _num_key(title)
        if key[1] is not None:
            meta_by_num.setdefault(key, vm)
        else:
            meta_by_series.setdefault(_series(title), vm)

    ordered = []
    used_meta = set()
    for v in meta['volume_list']:
        vid = v.get('volumeId', '')
        base = vid[:-5] if vid.endswith('.epub') else vid
        counts = {k: v.get(k) for k in TRANSLATIONS}

        # Reverse-look up volumes_meta
        vm = None
        nkey = _num_key(base)
        if nkey[1] is not None:
            vm = meta_by_num.get(nkey)
        if vm is None:
            vm = meta_by_exact.get(_norm(base))
        if vm is None:
            vm = meta_by_series.get(_series(base))   # volumeJp 无数字值兜底（如 'Secret Garden'）
        if vm is None:
            # Prefix fallback: volumeJp series is a prefix of an unlinked volumes_meta title
            sbase = _series(base)
            if len(sbase) >= 6:
                for ttt, mvm in meta_by_exact.items():
                    if ttt.startswith(sbase) and id(mvm) not in used_meta:
                        vm = mvm
                        break

        publish_at = None
        asin = None
        title = base
        if vm is not None and id(vm) not in used_meta:
            used_meta.add(id(vm))
            publish_at = vm.get('publishAt')
            asin = vm.get('asin')
            title = vm.get('title', base)

        ordered.append({
            'title': title,
            'volume_id': vid,
            'publish_at': publish_at,
            'asin': asin,
            'counts': counts,
        })

    # Sorted by publish_at (volumes without it stay put, appended at the end)
    ordered.sort(key=lambda x: (x['publish_at'] is None, x['publish_at'] or 0))

    for i, vol in enumerate(ordered, 1):
        vol['index'] = i
    return ordered


def download_volume(wid, volume_id, out_path, mode, translations, translations_mode,
                    session, stream=True):
    """Download one wenku volume as a complete EPUB.

    Returns (ok: bool, msg: str).
    """
    # filename per frontend format: {mode}.{Y|B}{translation-initial}.{volumeId}
    mark = 'B' if translations_mode == 'parallel' else 'Y'
    initials = ''.join(t[0] for t in translations)
    filename = f'{mode}.{mark}{initials}.{volume_id}'

    params = [('mode', mode), ('translationsMode', translations_mode),
              ('filename', filename)]
    for t in translations:
        params.append(('translations', t))
    qs = urllib.parse.urlencode(params)
    url = f'{NOVELIA_API}/wenku/{wid}/file/{urllib.parse.quote(volume_id)}?{qs}'

    resp = session.get(url, timeout=180, stream=stream)
    if resp.status_code != 200:
        resp.close()
        return False, f'HTTP {resp.status_code}'

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if stream:
        with open(out_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    else:
        out_path.write_bytes(resp.content)
    resp.close()

    size = out_path.stat().st_size if out_path.exists() else 0
    if size == 0:
        return False, 'empty file'
    return True, f'{size/1024/1024:.1f} MB'


def safe_title(title, max_len=80):
    """Filesystem-safe directory name from a book title."""
    s = re.sub(r'[<>:"/\\|?*]', '_', title)
    s = re.sub(r'\s+', ' ', s).strip().rstrip('.')
    s = s[:max_len].rstrip('. ') or 'untitled'
    return s


def default_out_dir(title_zh):
    """Default output dir: {ebook_root}/{safe_title}."""
    fetch_dir = get_fetch_dir()
    ebook_root = fetch_dir.parent if fetch_dir.name == 'fetch' else fetch_dir
    return ebook_root / safe_title(title_zh)


def resolve_out_dir(base, wid):
    """If the target dir already belongs to a different wenku book, add _2/_3 suffix."""
    if not base.exists():
        return base
    info = base / 'book_info.json'
    if not info.exists():
        return base  # 无 book_info，视为可复用
    try:
        existing = json.loads(info.read_text(encoding='utf-8'))
        if existing.get('wid') == wid:
            return base
    except Exception:
        return base
    for i in range(2, 100):
        cand = Path(f'{base}_{i}')
        if not cand.exists():
            return cand
    return Path(f'{base}_{99}')


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Download novelia wenku (文库版) volumes as complete EPUBs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python wenku_fetch.py https://n.novelia.cc/wenku/<WID>            # 下载全部卷 (中文)
  python wenku_fetch.py https://n.novelia.cc/wenku/<WID> --list     # 只列卷不下
  python wenku_fetch.py https://n.novelia.cc/wenku/<WID> --volume 2 # 只下第2卷
  python wenku_fetch.py https://n.novelia.cc/wenku/<WID> --mode jp-zh -t sakura,gpt
        ''')
    parser.add_argument('url', nargs='?', help='novelia wenku URL, e.g. https://n.novelia.cc/wenku/<WID>')
    parser.add_argument('--wid', help='wenku id directly (24-hex) if no URL given')
    parser.add_argument('-o', '--out', help='Output directory (default: {ebook_root}/{title})')
    parser.add_argument('--mode', default='zh', choices=MODES,
                        help='Content mode (default: zh = Chinese only)')
    parser.add_argument('-t', '--translation', default=','.join(TRANSLATIONS),
                        help='Comma-separated translation priority, e.g. sakura,gpt,youdao '
                             '(default: all, priority auto-fallback)')
    parser.add_argument('--translations-mode', default='priority', choices=['priority', 'parallel'],
                        help='priority = first available translation; parallel = interleave (default: priority)')
    parser.add_argument('--volume', type=int, help='Only download this volume number (1-based)')
    parser.add_argument('--list', action='store_true', help='List volumes only, do not download')
    parser.add_argument('-w', '--workers', type=int, default=2,
                        help='Concurrent volume downloads (default: 2)')
    parser.add_argument('--force', action='store_true',
                        help='Re-download volumes that already exist (default: skip)')
    args = parser.parse_args()

    # Resolve wid
    if args.url:
        try:
            wid = parse_wenku_url(args.url)
        except ValueError as e:
            parser.error(str(e))
    elif args.wid:
        wid = re.sub(r'[^0-9a-fA-F]', '', args.wid)
        if len(wid) != 24:
            parser.error(f'Invalid wid: {args.wid} (expected 24-hex)')
        wid = wid.lower()
    else:
        parser.error('Either a wenku URL or --wid is required')

    translations = [t.strip().lower() for t in args.translation.split(',') if t.strip()]
    translations = [t for t in translations if t in TRANSLATIONS]
    if not translations:
        translations = list(TRANSLATIONS)

    session = create_session()

    print(f'Fetching wenku metadata: {wid}...')
    try:
        meta = fetch_metadata(wid, session)
    except RuntimeError as e:
        print(f'ERROR: {e}')
        sys.exit(1)

    print(f'  Title:  {meta["title_zh"]}')
    print(f'  JP:     {meta["title"]}')
    print(f'  Author: {", ".join(meta["authors"]) or "Unknown"}')
    if meta['publisher']:
        print(f'  Pub:    {meta["publisher"]} / {meta["imprint"]} ({meta["level"]})')
    if meta['web_ids']:
        print(f'  Web:    {", ".join(meta["web_ids"])}')

    volumes = _build_volumes(meta)
    if not volumes:
        print('ERROR: No volumes found for this wenku book.')
        sys.exit(1)

    print(f'\n  {len(volumes)} volume(s), mode={args.mode}, translation={translations}, '
          f'translations-mode={args.translations_mode}')
    for v in volumes:
        counts = ' '.join(f'{k}={v["counts"].get(k, 0)}' for k in TRANSLATIONS)
        pub = time.strftime('%Y-%m', time.localtime(v['publish_at'])) if v['publish_at'] else '?'
        print(f'    [{v["index"]}] ({pub}) {v["title"][:70]}  | {counts}')

    if args.list:
        print('\n  List mode: done (no download).')
        return

    # Resolve output directory
    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = resolve_out_dir(default_out_dir(meta['title_zh']), wid)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f'\n  Output dir: {out_dir}')

    # Select volumes
    targets = volumes
    if args.volume:
        sel = [v for v in volumes if v['index'] == args.volume]
        if not sel:
            print(f'ERROR: volume {args.volume} not found (valid: 1-{len(volumes)})')
            sys.exit(1)
        targets = sel

    def _one(vol):
        filename = f'第{vol["index"]}卷_{vol["volume_id"]}'
        out_path = out_dir / filename
        if out_path.exists() and not args.force:
            return vol, True, 'skipped (exists)', str(out_path)
        try:
            ok, msg = download_volume(wid, vol['volume_id'], out_path,
                                      args.mode, translations, args.translations_mode,
                                      session)
            return vol, ok, msg, str(out_path)
        except Exception as e:
            return vol, False, str(e), str(out_path)

    if args.workers > 1 and len(targets) > 1:
        print(f'\n  Downloading {len(targets)} volume(s) with {args.workers} workers...')
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(_one, v) for v in targets]
            for f in as_completed(futures):
                vol, ok, msg, path = f.result()
                status = 'OK' if ok else 'FAILED'
                print(f'    [{vol["index"]}/{len(volumes)}] 第{vol["index"]}卷 {status} ({msg})')
                print(f'      -> {path}')
    else:
        print(f'\n  Downloading {len(targets)} volume(s)...')
        for v in targets:
            vol, ok, msg, path = _one(v)
            status = 'OK' if ok else 'FAILED'
            print(f'    [{vol["index"]}/{len(volumes)}] 第{vol["index"]}卷 {status} ({msg})')
            if ok:
                print(f'      -> {path}')

    # Write book_info.json
    info = {
        'type': 'wenku',
        'wid': wid,
        'title': meta['title'],
        'title_zh': meta['title_zh'],
        'authors': meta['authors'],
        'publisher': meta['publisher'],
        'imprint': meta['imprint'],
        'level': meta['level'],
        'web_ids': meta['web_ids'],
        'mode': args.mode,
        'translations': translations,
        'translations_mode': args.translations_mode,
        'fetched_at': time.strftime('%Y-%m-%d %H:%M'),
        'volumes': [{'index': v['index'], 'title': v['title'],
                     'filename': f'第{v["index"]}卷_{v["volume_id"]}',
                     'publish_at': v['publish_at'], 'asin': v['asin']} for v in volumes],
        'web_version': None,
    }
    (out_dir / 'book_info.json').write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')

    _mark_wenku(wid, meta['title_zh'], len(volumes))
    print(f'\n  book_info.json saved. Total {len(volumes)} volume(s).')

    # Fetch-timeline record
    try:
        from fetch_history import record as _rec
        _rec('wenku', meta['title_zh'], wid, len(volumes), 'volumes', str(out_dir), 'wenku')
    except Exception:
        pass


if __name__ == '__main__':
    main()
