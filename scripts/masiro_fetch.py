#!/usr/bin/env python3
"""
真白萌 (masiro.me) 抓取器。

masiro 是 admin 后端站，页面对未登录用户重定向到 /admin/auth/login，且带 Cloudflare 盾。
方案：复用 wenku8_fetch.py 的 Edge CDP 骨架 → 真实 Edge 穿盾 → 注入登录态（表单 + 路径）
→ 抓目录 JSON（chapters-json / f-chapters-json 两个 script）→ 逐章取正文。

章节接口: /admin/novelReading?cid={cid}
目录:      /admin/novelView?novel_id={novel_id} 内嵌 <script id='chapters-json'/> 与 'f-chapters-json'
付费章节 (cost>0 且含 '立即打钱') 跳过，不购买。

用法:
  python masiro_fetch.py "https://masiro.me/admin/novelView?novel_id=<MSID>"
  python masiro_fetch.py --id <MSID> -o out.txt
需要 config.json 配置 masiro.username / masiro.password。
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

from wenku8_fetch import (kill_edge, launch_edge, is_cdp_alive, find_tab,
                          wait_for_page, ws_connect, cdp_eval, page_navigate)

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"


def _load_memory():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _mark_masiro(novel_id, title, chapter_count):
    try:
        data = _load_memory()
        key = f'masiro:{novel_id}'
        data[key] = {
            'title': title,
            'source': 'masiro',
            'kind': 'web',
            'chapter_count': chapter_count,
            'first_fetch': data.get(key, {}).get('first_fetch', time.strftime('%Y-%m-%d %H:%M')),
            'last_fetch': time.strftime('%Y-%m-%d %H:%M'),
        }
        _save_memory(data)
    except Exception:
        pass


def parse_url(url):
    m = re.search(r'novel_id=(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'(\d{5,})', url)
    if m:
        return m.group(1)
    raise ValueError(f'无法从 masiro URL 解析 novel_id: {url}')


def _el_text(ws, selector):
    """取 CSS 选择器的文本。"""
    js = ("(function(){ let el=document.querySelector(%r);"
          " return el ? (el.textContent||'').trim() : ''; })()") % selector
    return cdp_eval(ws, js)


def _read_script_json(ws, node_id):
    """读 <script id='%node_id'> 内的 JSON 文本。"""
    js = ("(function(){ let s=document.getElementById(%r);"
          " return s ? s.textContent : ''; })()") % node_id
    return cdp_eval(ws, js)


def _do_login(ws):
    """在页面注入账号密码并提交登录表单，等待跳转。"""
    username = get('masiro.username', '')
    password = get('masiro.password', '')
    if not username or not password:
        raise RuntimeError('真白萌需登录，请在 config.json 配置 masiro.username / masiro.password。')
    # Read the csrf-token (_token) input value
    token = cdp_eval(ws, "(function(){let el=document.querySelector("
                         "'input[name=\"_token\"],input[name=\"csrf-token\"],input[type=\"hidden\"]');"
                         " return el?el.value:'';})()")
    # Fill in credentials and submit
    js = ("(function(){"
          "function setVal(sel,v){let el=document.querySelector(sel);"
          " if(el){el.value=v; el.dispatchEvent(new Event('input',{bubbles:true}));}}"
          "setVal('input[name=\"username\"]',%s);"
          "setVal('input[name=\"password\"]',%s);"
          "let f=document.querySelector('form'); if(f) f.submit();})()") % (
              json.dumps(username), json.dumps(password))
    cdp_eval(ws, js)
    print('  已提交登录表单...', flush=True)
    time.sleep(4)
    # Re-navigate to the target page after login
    return token


def fetch_book(novel_id, visible=False):
    novel_url = f'https://masiro.me/admin/novelView?novel_id={novel_id}'
    kill_edge()
    launch_edge(novel_url, visible=visible)
    ws = None
    try:
        tab = wait_for_page('masiro.me', timeout=60)
        if not tab:
            raise RuntimeError('masiro.me 页面加载超时（Cloudflare/网络）')
        ws = ws_connect(tab['webSocketDebuggerUrl'])
        time.sleep(2)

        # May be redirected to the login page
        cur = cdp_eval(ws, 'location.href')
        if 'auth/login' in (cur or ''):
            _do_login(ws)
            page_navigate(ws, novel_url)
            time.sleep(4)

        # Read title/author
        title = _el_text(ws, '.novel-title') or _el_text(ws, 'h1')
        author = _el_text(ws, '.author a') or _el_text(ws, '.author')
        if not title:
            title = f'masiro{novel_id}'

        # TOC: two script JSON payloads
        chapters = []
        chap_json_raw = _read_script_json(ws, 'chapters-json')
        parent_raw = _read_script_json(ws, 'f-chapters-json')
        chap_list = []
        parent_list = []
        try:
            chap_list = json.loads(chap_json_raw) if chap_json_raw else []
        except Exception:
            pass
        try:
            parent_list = json.loads(parent_raw) if parent_raw else []
        except Exception:
            pass

        if not chap_list:
            # Fallback: read all novelReading links on the page
            print('  目录 JSON 为空，尝试从 DOM 提链接', flush=True)
            raw = cdp_eval(ws, '''
            (function(){
              let out=[];
              document.querySelectorAll('a[href*="novelReading"]').forEach(a=>{
                let m=(a.getAttribute('href')||'').match(/cid=(\\d+)/);
                if(m) out.push({cid:m[1], title:(a.textContent||'').trim()});
              });
              return JSON.stringify(out);
            })()
            ''')
            try:
                links = json.loads(raw) if raw else []
            except Exception:
                links = []
            for lk in links:
                chapters.append((len(chapters) + 1, lk['title'], lk['cid']))
        else:
            # Assemble by parent level (flatten)
            for ch in chap_list:
                cid = str(ch.get('id', ''))
                ch_title = ch.get('title', f'第{len(chapters)+1}节')
                if not cid:
                    continue
                chapters.append((len(chapters) + 1, ch_title, cid))

        if not chapters:
            raise RuntimeError('未提取到章节目录（可能登录失败或未授权）')
        print(f'  目录: {title}  | {len(chapters)} 章', flush=True)

        # Fetch content chapter by chapter
        content_map = []
        for idx, ch_title, cid in chapters:
            page_navigate(ws, f'https://masiro.me/admin/novelReading?cid={cid}')
            time.sleep(3)   # 防 ban 强制慢速
            body = cdp_eval(ws, '''
            (function(){ let el=document.querySelector('.box-body.nvl-content');
              return el ? el.innerText : (document.body.innerText||''); })()
            ''')
            if any(k in body for k in ['立即打钱', '登入後查看', '需購買', '付費']):
                print(f'  跳过(付费): {ch_title[:40]}', flush=True)
                continue
            if len(body.strip()) < 5:
                continue
            content_map.append((idx, ch_title, body))
            if idx % 10 == 0:
                print(f'  [{idx}/{len(chapters)}] {ch_title[:30]} OK', flush=True)
            time.sleep(3)

        if not content_map:
            raise RuntimeError('未抓到任何正文章节（可能全部付费）')
        return title, author, content_map
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Fetch masiro.me (真白萌) novel → EPUB',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python masiro_fetch.py "https://masiro.me/admin/novelView?novel_id=<MSID>"
  python masiro_fetch.py --id <MSID> -o out.txt
  python masiro_fetch.py --id <MSID> --visible
需要 config.json 配置 masiro.username / masiro.password。
        ''')
    parser.add_argument('url', nargs='?', help='masiro novel URL (novelView?novel_id=..)')
    parser.add_argument('--id', help='masiro novel_id')
    parser.add_argument('-o', '--output', help='Output file (.txt/.epub)')
    parser.add_argument('--title', help='EPUB title override')
    parser.add_argument('--author', help='EPUB author override')
    parser.add_argument('--visible', action='store_true', help='Show browser window')
    args = parser.parse_args()

    if args.id:
        novel_id = re.sub(r'\D', '', args.id)
    elif args.url:
        novel_id = parse_url(args.url)
    else:
        parser.error('需要 URL 或 --id')

    print(f'真白萌抓取: {novel_id}')
    title, author, chapters = fetch_book(novel_id, visible=args.visible)
    if not title:
        title = f'masiro{novel_id}'
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

    _mark_masiro(novel_id, title, len(chapters))
    try:
        from fetch_history import record as _rec
        _rec('masiro', title, novel_id, len(chapters), 'chapters', str(out_path), 'web')
    except Exception:
        pass

    print(f'输出: {out_path}')


if __name__ == '__main__':
    main()
