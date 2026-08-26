#!/usr/bin/env python3
"""
esjzone (www.esjzone.one) 抓取器。

esjzone 是 Discuz 论坛站，正文由客户端 JS 渲染（改版后 /detail/{id}.html 已失效），
书籍入口为 /forum/{board_id}/{thread_id}/。因此复用 wenku8_fetch.py 的 Edge CDP 骨架：
启动真实 Edge → 等 Cloudflare/页面加载 → CDP 取渲染后 DOM。

正文/章节在渲染后 DOM（帖内楼层或分页）。付费/密码章节跳过（不购买）。

用法:
  python esj_fetch.py "https://www.esjzone.one/forum/<BOARD>/<ESJID>/"
  python esj_fetch.py --id <ESJID> -o out.txt
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

# 复用 wenku8_fetch.py 的通用 CDP 骨架（Edge 启动/穿透/提取）
from wenku8_fetch import (kill_edge, launch_edge, is_cdp_alive, find_tab,
                          wait_for_page, ws_connect, cdp_eval, page_navigate)

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"

ESJ_BOARD = '1584622325'   # 原创小说板块


def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_esj(book_id, title, chapter_count):
    try:
        data = _load_memory()
        key = f'esj:{book_id}'
        data[key] = {
            'title': title,
            'source': 'esj',
            'kind': 'web',
            'chapter_count': chapter_count,
            'first_fetch': data.get(key, {}).get('first_fetch', time.strftime('%Y-%m-%d %H:%M')),
            'last_fetch': time.strftime('%Y-%m-%d %H:%M'),
        }
        _save_memory(data)
    except Exception:
        pass


def parse_url(url):
    """从 esjzone URL 提取 thread_id（书号）。"""
    m = re.search(r'/forum/\d+/(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'(\d{8,})', url)
    if m:
        return m.group(1)
    raise ValueError(f'无法从 esjzone URL 解析书号: {url}')


def get_board(url):
    """从 URL 提取 board_id，缺省用原创小说板块。"""
    m = re.search(r'/forum/(\d+)/', url)
    return m.group(1) if m else ESJ_BOARD


def _read_rendered(ws):
    """取当前页 innerText（正文 + 元数据）。"""
    return cdp_eval(ws, 'document.body ? document.body.innerText : ""')


def _extract_novel_links(ws):
    """从渲染后 DOM 提取书籍页内所有正文帖链接（{book_id}/{page_id}）。

    返回 [(url, title)]。Discuz 书籍页内正文常为分页楼层。
    """
    js = '''
    (function(){
      let out = [];
      document.querySelectorAll('a[href]').forEach(a=>{
        let h = a.getAttribute('href')||'';
        let m = h.match(/\\/forum\\/[0-9]+\\/([0-9]+)\\//);
        if(m){ out.push({url:h, title:(a.innerText||'').trim().substring(0,80)}); }
      });
      return JSON.stringify(out);
    })()
    '''
    raw = cdp_eval(ws, js)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def fetch_book(book_id, board=None, visible=False):
    """抓取一本书，返回 (title, author, chapters[(序号, 标题, 正文)])。"""
    board = board or ESJ_BOARD
    entry_url = f'https://www.esjzone.one/forum/{board}/{book_id}/'

    kill_edge()
    launch_edge(entry_url, visible=visible)
    try:
        tab = wait_for_page('esjzone.one', timeout=60)
        if not tab:
            raise RuntimeError('esjzone 页面加载超时（Cloudflare/网络）')
        ws = ws_connect(tab['webSocketDebuggerUrl'])
        time.sleep(2)  # 等 JS 渲染

        page_text = _read_rendered(ws)

        # 书名/作者：页面顶层文本 "书名 作者: xxx"
        title = ''
        author = ''
        m = re.search(r'^.*?([^\n]{2,60})', page_text)
        # 更稳：从 body 前两行找书名与作者 "作者: xxx"
        am = re.search(r'作者[:：]\s*([^\n]+)', page_text)
        if am:
            author = am.group(1).strip()
        # 书名取页面 <title> 或首行
        tm = cdp_eval(ws, '(document.title||"").split(" - ")[0].trim()')
        title = tm or (m.group(1).strip() if m else f'esj{book_id}')

        # 章节链接：渲染后 DOM 提取
        links = _extract_novel_links(ws)
        chapters = []
        for each in links:
            u = each['url']
            if board and f'/{board}/' not in u:
                continue
            mm = re.search(r'/forum/\d+/(\d+)', u)
            if not mm or mm.group(1) == book_id:
                continue   # 排除书籍页本身
            page_id = mm.group(1)
            full = u if u.startswith('http') else f'https://www.esjzone.one{u}'
            # 进正文
            page_navigate(ws, full)
            time.sleep(2)
            body = _read_rendered(ws)
            if len(body.strip()) < 10:
                continue
            # 密码/付费章节标记
            if any(k in body for k in ['btn-send-pw', '內文目前施工中', '登录后查看', '付费', '購買']):
                print(f'  跳过(密码/付费): {each["title"][:40]}', flush=True)
                continue
            chapters.append((len(chapters) + 1, each['title'] or f'第{len(chapters)+1}节', body))
            print(f'  [{len(chapters)}] {each["title"][:40]} OK ({len(body)} 字)', flush=True)
            time.sleep(0.5)

        if not chapters:
            # esjzone 改版后正文需登录可见（Discuz 帖内楼层）
            err = ('未抓到任何正文章节：esjzone 改版后正文需登录。\n'
                   '请在 config.json 配置 esj.username / esj.password（或 esj.cookie）。')
            raise RuntimeError(err)
        return title, author, chapters
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Fetch esjzone (www.esjzone.one) novel → EPUB',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python esj_fetch.py "https://www.esjzone.one/forum/<BOARD>/<ESJID>/"
  python esj_fetch.py --id <ESJID> -o out.txt
  python esj_fetch.py --id <ESJID> --visible        # 显示浏览器调试
        ''')
    parser.add_argument('url', nargs='?', help='esjzone book URL')
    parser.add_argument('--id', help='esjzone thread_id (书号)')
    parser.add_argument('-o', '--output', help='Output file (.txt/.epub)')
    parser.add_argument('--title', help='EPUB title override')
    parser.add_argument('--author', help='EPUB author override')
    parser.add_argument('--visible', action='store_true', help='Show browser window')
    args = parser.parse_args()

    if args.id:
        book_id = re.sub(r'\D', '', args.id)
        board = None
    elif args.url:
        book_id = parse_url(args.url)
        board = get_board(args.url)
    else:
        parser.error('需要 URL 或 --id')

    print(f'esjzone 抓取: {book_id}')
    title, author, chapters = fetch_book(book_id, board=board, visible=args.visible)
    if not title:
        title = f'esj{book_id}'
    if not author:
        author = ''

    parts = [f'# {title}']
    if author:
        parts.append(f'作者: {author}')
    parts.append('')
    for idx, ch_title, content in chapters:
        parts.append(f'\n\n第{idx}章 {ch_title}\n{content}\n')
    text = '\n'.join(parts)
    print(f'\n总字数: {len(text):,}')

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

    _mark_esj(book_id, title, len(chapters))
    try:
        from fetch_history import record as _rec
        _rec('esj', title, book_id, len(chapters), 'chapters', str(out_path), 'web')
    except Exception:
        pass

    print(f'输出: {out_path}')


if __name__ == '__main__':
    main()
