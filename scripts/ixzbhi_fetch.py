#!/usr/bin/env python3
"""
Download ready-made 轻小说文库 EPUBs from the ixinzhi GitHub repos.

These are community-made (布客新知) EPUBs, one file per 文库版 book, hosted as raw
files under github.com/ixinzhi/lightnovel-<year> (2009to2013..2025). Filename
format: '<书名> - <作者> - <YYYYMMDD>.epub'. Direct GitHub raw download — no
Cloudflare/login/password, unlike scraping wenku8.net directly.

Index is built once via the GitHub contents API (8 repos) and cached to CSV so
subsequent searches are offline. Matching is fuzzy (case/space/punctuation-blind
substring over title or author).

Usage:
  python ixzbhi_fetch.py "书名关键词"            # search + download best match
  python ixzbhi_fetch.py "书名" --list           # list matches, don't download
  python ixzbhi_fetch.py "书名" -o out.epub      # explicit output path
  python ixzbhi_fetch.py "书名" --rebuild        # force re-fetch index
"""

import sys, os, re, csv, io, json, argparse, unicodedata
from pathlib import Path

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get
import requests

YEARS = ['2009to2013', '2014to2017', '2018to2020', '2021', '2022', '2023', '2024', '2025']
# git/trees recursive API returns the FULL file list (no 1000-entry cap like contents/).
BRANCH = 'master'
TREES_API = 'https://api.github.com/repos/ixinzhi/lightnovel-{year}/git/trees/{branch}?recursive=1'
RAW_HEADERS = {'User-Agent': 'Mozilla/5.0 (txt-to-epub)', 'Accept': 'application/vnd.github.v3+json'}

FETCH_DIR = get('fetch_dir', './fetch')
INDEX_PATH = Path(FETCH_DIR) / 'ixzbhi_index.csv'

# Chars to ignore when matching. NFKC already folds most full-width to half-width,
# so strip a broad ASCII+CJK punctuation set, spaces, and line breaks.
_PUNCT = set(' \t\r\n　·．.，,、;；:：/\\()[]{}【】<>《》""\'‘’…—_※☆★!！?？~■□●')


def _norm(s):
    """Blind match key: NFKC-normalize, lowercase, strip punctuation + whitespace."""
    s = unicodedata.normalize('NFKC', s or '')
    s = s.lower()
    return ''.join(c for c in s if c not in _PUNCT)


def _parse_name(name):
    """'<书名> - <作者> - <YYYYMMDD>.epub' -> (title, author, date)."""
    base = name.rsplit('.', 1)[0]
    parts = base.split(' - ')
    if len(parts) >= 3:
        title = ' - '.join(parts[:-2])
        author = parts[-2]
        date = parts[-1]
    else:
        title, author, date = base, '', ''
    return title, author, date


def build_index(force=False):
    """Return list of dicts {year, name, title, author, date, url, size}."""
    if not force and Path(INDEX_PATH).exists():
        return load_index()
    rows = []
    for year in YEARS:
        try:
            r = requests.get(TREES_API.format(year=year, branch=BRANCH), headers=RAW_HEADERS, timeout=40)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f'  [warn] {year}: API failed ({e}), reuse cache if any', file=sys.stderr)
            continue
        tree = data.get('tree', [])
        count = 0
        for f in tree:
            if f.get('type') == 'blob' and str(f.get('path', '')).endswith('.epub'):
                name = os.path.basename(f['path'])
                title, author, date = _parse_name(name)
                rows.append({
                    'year': year, 'name': name, 'title': title,
                    'author': author, 'date': date,
                    'url': f'https://raw.githubusercontent.com/ixinzhi/lightnovel-{year}/{BRANCH}/{f["path"]}',
                    'size': f.get('size', 0),
                })
                count += 1
        print(f'  {year}: {count} epub files')
        import time
        time.sleep(0.5)
    Path(INDEX_PATH).parent.mkdir(parents=True, exist_ok=True)
    save_index(rows)
    return rows


def save_index(rows):
    with open(INDEX_PATH, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['year', 'name', 'title', 'author', 'date', 'url', 'size'])
        w.writeheader()
        w.writerows(rows)
    print(f'  index saved: {INDEX_PATH} ({len(rows)} entries)')


def load_index():
    with open(INDEX_PATH, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def search(query, rows):
    q = _norm(query)
    hits = []
    for row in rows:
        t = _norm(row['title'])
        a = _norm(row['author'])
        if q and (q in t or q in a):
            hits.append(row)
    # prefer exact/near-exact title, then largest size (usually full volume)
    hits.sort(key=lambda r: (0 if _norm(r['title']) == q else 1, -int(r['size'])))
    return hits


def download(row, out_path):
    url = row['url']
    # Raw GitHub URLs contain spaces + CJK; URL-encode path but keep scheme/host/dirs.
    url = re.sub(r' ', '%20', url)
    print(f'  downloading: {row["name"]} ({int(row["size"])/1024/1024:.1f} MB)', file=sys.stderr)
    r = requests.get(url, headers=RAW_HEADERS, timeout=120, stream=True)
    r.raise_for_status()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'wb') as f:
        for chunk in r.iter_content(1 << 20):
            if chunk:
                f.write(chunk)
    size = out_path.stat().st_size
    is_epub = open(out_path, 'rb').read(2) == b'PK'
    print(f'  saved: {out_path} ({size/1024/1024:.1f} MB, {"epub" if is_epub else "NOT-epub!"})', file=sys.stderr)
    if not is_epub:
        print(f'  [warn] output is not a valid EPUB (PK header missing)', file=sys.stderr)
    return out_path, is_epub


def main():
    ap = argparse.ArgumentParser(description='Download ready-made 轻小说文库 EPUB from ixinzhi GitHub repos')
    ap.add_argument('query', help='Book title or author keyword')
    ap.add_argument('-o', '--output', help='Output EPUB path (default: fetch_dir/<书名>.epub)')
    ap.add_argument('--list', action='store_true', help='List matches without downloading')
    ap.add_argument('--rebuild', action='store_true', help='Force re-fetch the index from GitHub')
    ap.add_argument('--offline', action='store_true', help='Use cached index only (no GitHub)')
    args = ap.parse_args()

    if args.offline and not Path(INDEX_PATH).exists():
        print('ERROR: --offline but no cached index; run once without --offline', file=sys.stderr)
        sys.exit(1)
    rows = load_index() if Path(INDEX_PATH).exists() and not args.rebuild else build_index(force=args.rebuild)
    if not rows:
        print('ERROR: index empty; try --rebuild', file=sys.stderr)
        sys.exit(1)

    hits = search(args.query, rows)
    if not hits:
        print(f'No match for "{args.query}" in {len(rows)} books.')
        print('  Tip: try fewer keywords, or `--rebuild` to refresh the index.')
        sys.exit(0)

    if args.list or len(hits) > 1:
        print(f'{len(hits)} match(es) for "{args.query}":')
        for i, h in enumerate(hits[:10]):
            print(f'  [{i}] ({h["year"]}) {h["title"]} / {h["author"]} / {h["date"]}  ({int(h["size"])/1024/1024:.1f} MB)')

    pick = hits[0]
    if args.list:
        return
    if args.output:
        out = Path(args.output)
    else:
        out = Path(FETCH_DIR) / (pick['title'].strip() + '.epub')
    download(pick, out)
    print(f'\nBEST MATCH: {pick["title"]} / {pick["author"]} / {pick["date"]} ({pick["year"]})')


if __name__ == '__main__':
    main()
