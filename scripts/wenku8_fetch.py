#!/usr/bin/env python3
"""
Wenku8.net fetcher — pure HTTP (no browser/CDP).

Reads the PHP endpoints that render the same pages the app-style clients use:
  TOC:     GET {node}/modules/article/reader.php?aid={aid}&charset=gbk
  chapter: GET {node}/modules/article/reader.php?aid={aid}&cid={cid}&charset=gbk
Responses are GBK-encoded server-rendered HTML; content lives in <div id="content">.
Nodes www.wenku8.net / www.wenku8.cc are tried in order (Cloudflare 403s are handled
by curl_cffi impersonation in the access layer).

Usage:
  python wenku8_fetch.py -u https://www.wenku8.net/novel/<CAT>/<ID>/index.htm
  python wenku8_fetch.py -u ... --html "D:/output/dir"   # HTML mode with illustrations
  python wenku8_fetch.py -u ... --start 1 --end 5
"""

import re, sys, io, json, time, argparse, requests
from pathlib import Path

try:
    import requests as _requests
except ImportError:
    _requests = None
try:
    from curl_cffi import requests as curl_requests
    _has_curl = True
except ImportError:
    _has_curl = False
try:
    from bs4 import BeautifulSoup
    _has_soup = True
except ImportError:
    _has_soup = False

import urllib3
urllib3.disable_warnings()

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

_NODES = ["https://www.wenku8.net", "https://www.wenku8.cc"]
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0')
_HEADERS = {'User-Agent': _UA, 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'zh-CN,zh;q=0.9'}


# ============================================================
#  Fetch layer (GBK HTML)
# ============================================================

def _get_html(url):
    """GET a URL, follow redirects, decode GBK -> HTML string.

    Uses curl_cffi (browser TLS) when available — the wenku8 nodes are behind
    Cloudflare and occasionally reject the stdlib requests TLS fingerprint.
    """
    last = None
    for attempt in range(3):
        try:
            if _has_curl:
                r = curl_requests.get(url, headers=_HEADERS, verify=False, timeout=25)
            else:
                r = _requests.get(url, headers=_HEADERS, verify=False, timeout=25)
            if r.status_code == 403 and _has_curl:
                # stdlib impersonation blocked -> retry via curl_cffi already handled above
                pass
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} for {url}")
            return r.content.decode('gbk', errors='replace')
        except Exception as e:
            last = e
            time.sleep(0.8)
    raise last


def _get_with_failover(path):
    """Try each wenku8 node in order until one returns usable HTML."""
    for node in _NODES:
        try:
            html = _get_html(node + path)
            if 'content' in html or 'css' in html:
                return html
        except Exception as e:
            print(f"  {node} failed: {type(e).__name__}: {e}", file=sys.stderr)
    raise RuntimeError("all wenku8 nodes failed")


# ============================================================
#  Parsers
# ============================================================

def _err(html):
    s = BeautifulSoup(html, 'html.parser')
    bt = s.find(class_='blocktitle')
    if bt and bt.get_text(strip=True) in ('出现错误！', '出現錯誤！'):
        return bt.get_text(strip=True)
    return None


def fetch_catalogue(aid):
    """Return (title, chapters) where chapters = [(cid, title), ...] grouped in order."""
    html = _get_with_failover(f"/modules/article/reader.php?aid={aid}&charset=gbk")
    if _err(html):
        raise RuntimeError(f"wenku8 error: {_err(html)}")
    s = BeautifulSoup(html, 'html.parser')
    title = ''
    t = s.find('title')
    if t:
        title = re.sub(r'小说在线阅读与TXT电子书下载.*$', '', t.get_text()) or ''
    table = s.find('table', class_='css')
    chapters = []
    if table is None:
        # Fallback: some TOC layouts use .tablelist
        table = s.find(class_='tablelist') or s.find('table')
    if table is not None:
        seen = set()
        for row in table.find_all('tr'):
            # volume header
            vol_td = row.find('td', class_='vcss')
            if vol_td is not None:
                continue
            for td in row.find_all('td', class_='ccss'):
                a = td.find('a', href=True)
                if a is None:
                    continue
                ctitle = a.get_text(strip=True)
                href = (a['href'] or '').strip()
                if not ctitle or '&cid=' not in href:
                    continue
                cid = href.split('&cid=')[-1]
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                chapters.append((cid, ctitle))
    if not chapters:
        # last resort: any link with &cid=
        for a in s.find_all('a', href=True):
            if '&cid=' in a['href']:
                cid = a['href'].split('&cid=')[-1]
                ct = a.get_text(strip=True)
                if ct and cid not in [c for c, _ in chapters]:
                    chapters.append((cid, ct))
    return title, chapters


def _clean_content(html_frag):
    """Clean an extracted content fragment for HTML-mode output (keep structure)."""
    frag = re.sub(r'<ul[^>]*id="[^"]*contentdp[^"]*"[^>]*>.*?</ul>', '', html_frag, flags=re.DOTALL)
    frag = re.sub(r'本文来自.*?轻小说文库.*?</[^>]+>\s*', '', frag, flags=re.DOTALL)
    frag = re.sub(r'最新最全的日本动漫轻小说.*?一网打尽！\s*', '', frag)
    frag = re.sub(r'轻小说文库.*?內容報錯\s*', '', frag, flags=re.DOTALL)
    frag = re.sub(r'<div[^>]*id="[^"]*ad[^"]*"[^>]*>.*?</div>', '', frag, flags=re.DOTALL)
    # XHTML-ify void elements
    frag = re.sub(r'&nbsp;', '&#160;', frag)
    frag = re.sub(r'<br\b([^>]*)>', r'<br\1/>', frag)
    frag = re.sub(r'<hr\b([^>]*)>', r'<hr\1/>', frag)
    frag = re.sub(r'<img\b([^>]*[^/])>', r'<img\1/>', frag)
    return frag


def fetch_content(aid, cid):
    """Return (body_html, text, image_urls). body_html keeps <p>/<br>/<img> for HTML mode."""
    html = _get_with_failover(f"/modules/article/reader.php?aid={aid}&cid={cid}&charset=gbk")
    if _err(html):
        raise RuntimeError(f"wenku8 error: {_err(html)}")
    s = BeautifulSoup(html, 'html.parser')
    c = s.find(id='content')
    if c is None:
        raise RuntimeError('no #content in chapter page')
    ul = c.find('ul', id='contentdp')
    if ul:
        ul.decompose()
    imgs = [img.get('src') for img in c.find_all('img') if img.get('src')]
    for img in c.find_all('img'):
        img['src'] = _norm_img_url(img.get('src', ''))
    inner = c.decode_contents()
    body_html = _clean_content(inner)
    text = c.get_text('\n')
    text = re.sub(r'\s+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return body_html, text, [_norm_img_url(u) for u in imgs if u]


def _norm_img_url(u):
    u = u.replace('http://', 'https://')
    if u.startswith('/'):
        return 'https://www.wenku8.net' + u
    return u


def _is_illust(title):
    return any(k in title for k in ('插图', '插畫', 'イラスト'))


def fetch_detail(aid):
    """Return (title, author) — best effort from articleinfo.php / reader.php."""
    try:
        html = _get_with_failover(f"/modules/article/articleinfo.php?id={aid}&charset=gbk")
        s = BeautifulSoup(html, 'html.parser')
        title, author = '', ''
        t = s.find('title')
        if t:
            title = re.sub(r'小说在线阅读与TXT电子书下载.*$', '', t.get_text()) or title
        # author anywhere in the page ('作者：xxx' / '作者:xxx')
        for m in re.finditer(r'作者\s*[：:]\s*([^\s<&]{1,40})', html):
            a = m.group(1).strip()
            # skip if the match is a CSS/homepage noise
            if a and not re.match(r'^[a-z]+$', a, re.I) and 'wenku8' not in a:
                author = a
                break
        return title, author
    except Exception:
        return '', ''


# ============================================================
#  Memory
# ============================================================

def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_chapter(novel_id, ch_id, novel_title):
    data = _load_memory()
    key = f'wenku8:{novel_id}'
    data.setdefault(key, {'title': novel_title, 'chapters': {}, 'first_fetch': time.strftime('%Y-%m-%d %H:%M')})
    data[key]['chapters'][str(ch_id)] = time.strftime('%Y-%m-%d %H:%M')
    data[key]['last_fetch'] = time.strftime('%Y-%m-%d %H:%M')
    _save_memory(data)


def _get_fetched(novel_id):
    data = _load_memory()
    key = f'wenku8:{novel_id}'
    return set(data.get(key, {}).get('chapters', {}).keys())


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


def save_html_chapter(html_dir, ch_num, ch_title, body_html):
    html_dir = Path(html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', ch_title or f'chapter{ch_num}')
    fname = f"{ch_num:04d}_{safe_title}.html"
    path = html_dir / fname
    full = _HTML_PAGE.format(title=ch_title or f'Chapter {ch_num}', body=body_html)
    path.write_text(full, encoding='utf-8')
    return fname


def download_image(url, img_dir, fname):
    img_dir = Path(img_dir)
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / fname
    if path.exists():
        return fname
    try:
        if _has_curl:
            r = curl_requests.get(url, headers=_HEADERS, verify=False, timeout=20)
        else:
            r = _requests.get(url, headers=_HEADERS, verify=False, timeout=20)
        if r.status_code == 200 and len(r.content) > 100:
            path.write_bytes(r.content)
            return fname
    except Exception as e:
        print(f"  image FAILED: {fname} - {e}", file=sys.stderr)
    return None


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Fetch wenku8.net novel via pure HTTP')
    parser.add_argument('-u', '--url', required=True, help='Index page URL')
    parser.add_argument('-o', '--output', help='Output TXT file')
    parser.add_argument('--html', help='Output per-chapter HTML files to this directory')
    parser.add_argument('-d', '--delay', type=float, default=0.8, help='Delay between chapters')
    parser.add_argument('--start', type=int, default=1, help='Start chapter index')
    parser.add_argument('--end', type=int, default=0, help='End chapter index (0=all)')
    parser.add_argument('--force', action='store_true', help='Re-fetch even if in memory')
    args = parser.parse_args()

    if not _has_soup:
        print("ERROR: pip install beautifulsoup4", file=sys.stderr)
        sys.exit(1)

    index_url = args.url
    html_mode = bool(args.html)

    is_book_page = bool(re.match(r'https?://[^/]+/book/\d+\.html?', index_url))
    if is_book_page:
        novel_id = re.match(r'https?://[^/]+/book/(\d+)\.html?', index_url).group(1)
    else:
        m = re.search(r'/novel/\d+/(\d+)/', index_url)
        novel_id = m.group(1) if m else re.search(r'reader\.php\?aid=(\d+)', index_url).group(1)

    print(f"Novel ID (aid): {novel_id}")

    det_title, author = fetch_detail(novel_id)
    title, chapters = fetch_catalogue(novel_id)
    if not chapters:
        print("ERROR: no chapters found", file=sys.stderr)
        sys.exit(1)
    title = det_title or title or novel_id
    author = author or '未知'
    print(f"Title: {title}")
    print(f"Author: {author}")
    print(f"Found {len(chapters)} chapters")

    total = len(chapters)
    start_idx = max(0, args.start - 1)
    end_idx = min(total, args.end) if args.end > 0 else total
    chapters = chapters[start_idx:end_idx]

    # HTML mode setup
    html_output_dir = None
    img_dir = None
    cover_file = ''
    if html_mode:
        html_output_dir = Path(args.html)
        html_output_dir.mkdir(parents=True, exist_ok=True)
        img_dir = html_output_dir / 'images'
        img_dir.mkdir(parents=True, exist_ok=True)
        chapters_info = []
        cover_url = f'https://img.wenku8.com/image/0/{novel_id}/{novel_id}s.jpg'
        try:
            ck = download_image(cover_url, img_dir, f'{novel_id}s.jpg')
            if ck:
                cover_file = ck
                print(f"Cover: {cover_file}")
        except Exception as e:
            print(f"Cover: download failed - {e}")

    parts = [f"# {title}", f"作者: {author}", '', '']
    fetched_set = _get_fetched(novel_id) if not args.force else set()

    for i, (cid, ch_title) in enumerate(chapters):
        ch_num = start_idx + i + 1
        prefix = f"  [{ch_num}/{total}]"
        if cid in fetched_set:
            print(f"{prefix} (skip)", flush=True)
            continue
        print(f"{prefix} {ch_title[:40]}...", end=' ', flush=True)

        try:
            body_html, text, imgs = fetch_content(novel_id, cid)
            if not text and not imgs:
                print('(empty)')
                continue

            if html_mode:
                if _is_illust(ch_title) and imgs:
                    downloaded = []
                    for j, u in enumerate(imgs):
                        ext = '.jpg'
                        if 'png' in u: ext = '.png'
                        else:
                            from urllib.parse import urlparse
                            p = urlparse(u).path
                            if p.lower().endswith(('.png', '.webp', '.gif', '.svg')):
                                ext = p[p.rfind('.'):]
                        fname = f"ill_{cid}_{j}{ext}"
                        if download_image(u, img_dir, fname):
                            downloaded.append(fname)
                    imgs_html = ''.join(
                        f'<img src="images/{f}" alt="插图" style="max-width:100%;height:auto;display:block;margin:1em auto;">\n'
                        for f in downloaded
                    )
                    body = f'<div class="illustrations">{imgs_html}</div>'
                else:
                    body = f'<div class="chapter">{body_html}</div>'
                fname = save_html_chapter(html_output_dir, ch_num, ch_title, body)
                chapters_info.append({'num': ch_num, 'title': ch_title, 'file': fname,
                                      'type': 'illustration' if _is_illust(ch_title) else 'chapter'})
                print(f"OK ({len(imgs)} imgs)" if imgs else "OK")
            else:
                content = re.sub(r'本文来自.*?轻小说文库.*?\n', '', text)
                content = re.sub(r'最新最全的日本动漫轻小说.*?一网打尽！\s*', '', content)
                content = re.sub(r'轻小说文库.*?內容報錯\s*', '', content)
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
        if cover_file:
            cover_body = f'<div style="text-align:center;padding:2em 0;"><img src="images/{cover_file}" alt="封面" style="max-width:100%;height:auto;"/></div>'
            fname = save_html_chapter(html_output_dir, 0, '封面', cover_body)
            chapters_info.insert(0, {'num': 0, 'title': '封面', 'file': fname, 'type': 'cover'})
        info = {'book_name': title, 'author': author, 'cover': cover_file,
                'chapters': chapters_info}
        info_path = html_output_dir / 'book_info.json'
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\nMetadata: {info_path}")
        print(f"HTML chapters saved to: {html_output_dir}")
        print(f"Total: {len(chapters_info)} chapters")
        print("Next: python html2epub_font.py \"{html_output_dir}\" -o \"output.epub\"")
    else:
        if not args.output:
            safe = re.sub(r'[^\w一-鿿]', '_', title).strip('_') or 'wenku8'
            args.output = f'{safe}.txt'
        combined = '\n'.join(parts)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(combined)
        print(f"\nSaved: {args.output} ({len(combined):,} chars)")

    for cid, _ in chapters:
        _mark_chapter(novel_id, cid, title)
    try:
        from fetch_history import record as _rec
        _rec('wenku8', title, novel_id, len(chapters), 'chapters',
             args.html or args.output, 'web')
    except Exception:
        pass
    print("Done!")


if __name__ == '__main__':
    main()
