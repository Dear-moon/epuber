#!/usr/bin/env python3
"""
轻之国度（lightnovel.fun / 轻国）抓取器。

逆向自 https://github.com/ilusrdbb/lightnovel-pydownloader 的 app API：
  POST {domain}/api/user/login          登录 → {uid, security_key}（正文接口需登录）
  POST {domain}/api/series/get-info     合集元数据（id < 100000，免登录）
  POST {domain}/api/article/get-detail  单本/章节正文（id >= 100000 为单本）
响应为 base64 → zlib → JSON；正文为 BBCode，需清洗。
请求用 curl_cffi（python requests 连该站会 SSL EOF）。

用法:
  python lk_fetch.py "https://www.lightnovel.fun/<LKID>"         # 合集或单本
  python lk_fetch.py --id <LKID> -o out.txt
需要 config.json 里配置 lk.username / lk.password。
"""

import re
import sys
import io
import json
import time
import argparse
import subprocess
from pathlib import Path

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from config import get_fetch_dir, get
except ImportError:
    def get_fetch_dir():
        return Path.cwd() / "fetch"

    def get(key, default=None):
        return default

try:
    import zlib, base64
    from curl_cffi import requests as _cr
except ImportError:
    print("Error: 'curl_cffi' required. Install: pip install curl_cffi", file=sys.stderr)
    sys.exit(1)

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"


def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_lk(book_id, title, chapter_count):
    try:
        data = _load_memory()
        key = f'lk:{book_id}'
        data[key] = {
            'title': title,
            'source': 'lk',
            'kind': 'web',
            'chapter_count': chapter_count,
            'first_fetch': data.get(key, {}).get('first_fetch', time.strftime('%Y-%m-%d %H:%M')),
            'last_fetch': time.strftime('%Y-%m-%d %H:%M'),
        }
        _save_memory(data)
    except Exception:
        pass


# ============================================================
#  API client
# ============================================================

def _unzip(text):
    """base64 → zlib → JSON."""
    if not text:
        return None
    return json.loads(zlib.decompress(base64.b64decode(text)).decode('utf-8', 'replace'))


class LKClient:
    def __init__(self):
        self.domain = get('lk.domain', 'https://api.lightnovel.fun').rstrip('/')
        self.header = {
            'content-type': 'application/json; charset=UTF-8',
            'accept-encoding': 'gzip',
            'user-agent': 'Dart/2.10 (dart:io)',
        }
        self.uid = ''
        self.security_key = ''

    def _post(self, path, d, extra=None, retries=3):
        body = {
            'platform': 'android',
            'client': 'app',
            'sign': '',
            'ver_name': '0.11.52',
            'ver_code': '192',
            'd': d,
            'gz': 1,
        }
        if extra:
            body.update(extra)  # 顶层附加字段（如登录用的 is_encrypted）
        url = f'{self.domain}{path}'
        for attempt in range(retries):
            try:
                r = _cr.post(url, json=body, headers=self.header, timeout=25)
                data = _unzip(r.content) if r.content else None
                if data is not None:
                    return data
            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError(f'LK API 请求失败 {path}: {e}')
                time.sleep(1.0)
        raise RuntimeError(f'LK API 无响应: {path}')

    def login(self):
        username = get('lk.username', '')
        password = get('lk.password', '')
        if not username or not password:
            raise RuntimeError(
                '轻之国度需登录，请在 config.json 配置 lk.username / lk.password。'
                '(在 skill 目录的 config.json 中添加 "lk": {"username":"...", "password":"..."})'
            )
        print('  登录轻之国度...', flush=True)
        data = self._post('/api/user/login', {
            'username': username,
            'password': password,
        }, extra={'is_encrypted': 0})
        if data.get('code') != 0:
            raise RuntimeError(f'轻国登录失败: code={data.get("code")} {data.get("msg") or ""}')
        dd = data.get('data') or {}
        self.uid = str(dd.get('uid', ''))
        self.security_key = str(dd.get('security_key', ''))
        print(f'  登录成功 (uid={self.uid})', flush=True)

    def _auth(self):
        return {'uid': self.uid, 'security_key': self.security_key}

    def get_series(self, sid):
        d = {'sid': sid}
        d.update(self._auth())
        data = self._post('/api/series/get-info', d)
        if data.get('code') != 0:
            raise RuntimeError(f'合集信息失败: code={data.get("code")}')
        return data.get('data') or {}

    def get_detail(self, aid):
        d = {'aid': aid, 'simple': 0}
        d.update(self._auth())
        data = self._post('/api/article/get-detail', d)
        return data


def parse_url(url):
    """从 URL 提取轻国书号（第一个数字串）。"""
    m = re.search(r'\d+', url)
    if not m:
        raise ValueError(f'无法从 URL 解析轻国书号: {url}')
    return m.group(0)


def bbcode_to_text(text, detail):
    """清洗轻国 BBCode 正文 → 纯文本。插图(res/attach/img)丢弃。"""
    result = text or ''
    # [res]key[/res]: illustration, drop
    if detail and detail.get('res') and detail['res'].get('res_info'):
        result = re.sub(r'\[res\].*?\[/res\]', '', result)
    # [attach]key[/attach]: illustration, drop
    if detail and detail.get('attaches') and detail['attaches'].get('res_info'):
        result = re.sub(r'\[attach\].*?\[/attach\]', '', result)
    # [img]url[/img]: illustration, drop
    result = re.sub(r'\[img\].*?\[/img\]', '', result)
    # Strip all remaining bbcode tags
    result = re.sub(r'\[.*?\]', '', result)
    return result


def fetch_book(book_id):
    """抓取一本书，返回 (title, author, chapters[(序号, 标题, 正文)])。

    书号判断不能靠阈值（站点已无单本/合集分界）：先试单本 get-detail，
    无正文（合集返回 code 8 等）再退化为合集 get-info。
    """
    client = LKClient()
    client.login()

    title = author = ''
    chapters = []

    # ---- Try single-volume first ----
    detail = client.get_detail(book_id)
    dd = detail.get('data') or {}
    if detail.get('code') == 0 and dd.get('content'):
        title = dd.get('title', '')
        pay = dd.get('pay_info')
        if pay and pay.get('is_paid') == 0:
            raise RuntimeError('该内容为付费章节，已跳过（不自动购买）')
        content = bbcode_to_text(dd.get('content'), dd)
        chapters.append((1, title, content))
        print(f'  单本: {title}  | 1 篇 ({len(content)} 字)', flush=True)
        return title, author, chapters

    # ---- Otherwise handle as a collection ----
    info = client.get_series(book_id)
    title = info.get('name', '')
    author = info.get('author', '') or info.get('creator') or ''
    intro = info.get('intro', '')
    articles = info.get('articles') or []
    print(f'  合集: {title or book_id}  | 章节 {len(articles)} 篇', flush=True)
    for i, art in enumerate(articles, 1):
        aid = art.get('aid')
        ch_title = art.get('title', f'第{i}篇')
        detail = client.get_detail(aid)
        if detail.get('code') != 0:
            print(f'    [{i}/{len(articles)}] {ch_title[:40]} 获取失败 code={detail.get("code")}', flush=True)
            continue
        dd = detail.get('data') or {}
        pay = dd.get('pay_info')
        if pay and pay.get('is_paid') == 0:
            print(f'    [{i}/{len(articles)}] {ch_title[:40]} 付费章节，跳过（不自动购买）', flush=True)
            continue
        content = bbcode_to_text(dd.get('content'), dd)
        if not content.strip():
            print(f'    [{i}/{len(articles)}] {ch_title[:40]} 空内容，跳过', flush=True)
            continue
        chapters.append((i, ch_title, content))
        print(f'    [{i}/{len(articles)}] {ch_title[:40]} OK ({len(content)} 字)', flush=True)
        time.sleep(0.3)
    if intro:
        chapters.insert(0, (0, '简介', intro))

    if not chapters:
        raise RuntimeError('没有抓到任何章节内容（可能全部为付费章节）')
    if not title:
        title = f'轻国{book_id}'
    return title, author, chapters


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Fetch lightnovel.fun (轻之国度) novel and convert to EPUB',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python lk_fetch.py https://www.lightnovel.fun/<LKID>
  python lk_fetch.py --id <LKID> -o out.txt
  python lk_fetch.py --id <LKID> -o out.epub --title "书名"
        ''')
    parser.add_argument('url', nargs='?', help='lightnovel.fun book URL (any URL containing the book id)')
    parser.add_argument('--id', help='轻国书号 directly')
    parser.add_argument('-o', '--output', help='Output file (.txt or .epub)')
    parser.add_argument('--title', help='EPUB title override')
    parser.add_argument('--author', help='EPUB author override')
    args = parser.parse_args()

    if args.url:
        try:
            book_id = parse_url(args.url)
        except ValueError as e:
            parser.error(str(e))
    elif args.id:
        book_id = re.sub(r'\D', '', args.id)
    else:
        parser.error('需要 URL 或 --id')

    print(f'轻之国度抓取: {book_id}')
    try:
        title, author, chapters = fetch_book(book_id)
    except RuntimeError as e:
        print(f'ERROR: {e}')
        sys.exit(1)
    if not title:
        title = f'轻国{book_id}'
    if not author:
        author = ''

    # Assemble TXT
    parts = [f'# {title}']
    if author:
        parts.append(f'作者: {author}')
    parts.append('')
    for idx, ch_title, content in chapters:
        if idx == 0:
            parts.append(f'\n简介\n{content}\n')
        else:
            parts.append(f'\n\n第{idx}章 {ch_title}\n{content}\n')
    text = '\n'.join(parts)
    print(f'\n总字数: {len(text):,}')

    # Output: auto-convert to EPUB under ebook root by default; -o .txt for plain text
    fetch_dir = get_fetch_dir()
    ebook_root = fetch_dir.parent if fetch_dir.name == 'fetch' else fetch_dir
    safe = re.sub(r'[<>:"/\\|?*]', '_', title)[:80]

    if args.output and Path(args.output).suffix.lower() != '.epub':
        out_path = Path(args.output)
        out_path.write_text(text, encoding='utf-8')
    else:
        out_path = Path(args.output) if args.output else (ebook_root / f'{safe}.epub')
        tmp = out_path.with_suffix('.txt')
        tmp.write_text(text, encoding='utf-8')
        subprocess.run([sys.executable, str(Path(__file__).parent / 'convert.py'),
                        str(tmp), '-o', str(out_path), '--title', title]
                       + (['--author', author] if author else []))
        try:
            tmp.unlink()
        except Exception:
            pass

    # Memory + fetch-timeline record
    _mark_lk(book_id, title, len([c for c in chapters if c[0] != 0]))
    try:
        from fetch_history import record as _rec
        _rec('lk', title, book_id, len([c for c in chapters if c[0] != 0]),
             'chapters', str(out_path), 'web')
    except Exception:
        pass

    print(f'输出: {out_path}')


if __name__ == '__main__':
    main()
