#!/usr/bin/env python3
"""
lightnovel.app Refresh Token 自动获取（优先无浏览器磁盘读）。

登录 lightnovel.app 后，token 作为裸字符串存在 profile 的 IndexedDB leveldb
（LightNovelShelf / USER_AUTHENTICATION / 'RefreshToken'）里。

主路径**不开浏览器**：直接解析磁盘 leveldb `.log`（WriteBatch 记录，key 含
UTF-16LE "RefreshToken"）抠出 token，profile 被占用也能读。
回退路径：复用已登录 profile，用 Edge/Chrome CDP 读 IndexedDB（此路径需先关闭
该浏览器，复用 profile 无法双开）。

用法:
  python refresh_token.py                # 磁盘读 → 写回 config.json
  python refresh_token.py --print        # 只打印不写
  python refresh_token.py --profile <路径>   # 显式指定浏览器 profile
  python refresh_token.py --browser chrome  # 强制 Chrome（默认自动探测）
"""

import os
import sys
import io
import json
import re
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
    from config import get
except ImportError:
    def get(key, default=None):
        return default

# 复用 wenku8_fetch 的通用 CDP 骨架（wenku8_fetch 已改纯 HTTP，CDP 助手可能不存在；
# 主路径为磁盘读，CDP 仅浏览器回退时用，故惰性引入、缺省降级为 None）。
try:
    from wenku8_fetch import wait_for_page, ws_connect, cdp_eval
except ImportError:
    wait_for_page = ws_connect = cdp_eval = None

SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_ROOT / 'config.json'

DB_NAME = 'LightNovelShelf'
STORE_NAME = 'USER_AUTHENTICATION'
KEY = 'RefreshToken'
ORIGIN_MARK = 'lightnovel'   # profile IndexedDB 目录名含此即视为登录过该站


# ---- 浏览器可执行路径 ----

def _edge_exe():
    from config import get_edge_path
    p = get_edge_path()
    if p and Path(str(p)).exists():
        return str(p)
    return None


def _chrome_exe():
    cands = [
        os.path.join(os.environ.get('PROGRAMFILES', r'C:\Program Files'), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
    ]
    for c in cands:
        if Path(c).exists():
            return c
    return None


# ---- 探测 profile ----

def _profile_roots():
    la = os.environ.get('LOCALAPPDATA', '')
    return {
        'edge': Path(la) / 'Microsoft' / 'Edge' / 'User Data',
        'chrome': Path(la) / 'Google' / 'Chrome' / 'User Data',
    }


def _has_lightnovel_indexeddb(profile_dir):
    """profile 的 IndexedDB 目录下是否有含 'lightnovel' 的 leveldb（按 origin 命名）。"""
    idb = Path(profile_dir) / 'IndexedDB'
    if not idb.is_dir():
        return False
    try:
        for d in idb.iterdir():
            name = d.name.lower()
            if d.is_dir() and (ORIGIN_MARK in name or 'lightnovel.app' in name):
                return True
    except Exception:
        pass
    return False


def _list_profiles(user_data_root):
    """列出 User Data 下所有 profile 名（相对名）。"""
    if not user_data_root.is_dir():
        return []
    names = []
    for d in user_data_root.iterdir():
        if d.is_dir() and (d.name == 'Default' or d.name.startswith('Profile') or d.name.startswith('Person')):
            names.append(d.name)
    names.sort(key=lambda n: (n != 'Default', n))
    return names


def detect_profile(browser_pref=None, explicit_path=None):
    """探测含 lightnovel.app IndexedDB 的浏览器 profile。

    Returns (browser, user_data_root: Path, profile_name: str) or raises RuntimeError.

    注意：--user-data-dir 必须指向 User Data 根，profile 用 --profile-directory 指定，
    否则 Chromium 会把根子目录当新 user data，读到空 profile。
    """
    if explicit_path:
        # 显式路径可能给到根或给到 profile 子目录；统一解析
        p = Path(explicit_path)
        # 若 p 直接是含 IndexedDB 的 profile（其下 IndexedDB 有 lightnovel）
        if _has_lightnovel_indexeddb(p):
            return ('explicit', p.parent, p.name)
        # 若 p 是 User Data 根，找其下含 lightnovel 的 profile 子目录
        for name in _list_profiles(p):
            prof_dir = p / name
            if _has_lightnovel_indexeddb(prof_dir):
                return ('explicit', p, name)
        raise RuntimeError(f'profile {p} 下未找到 lightnovel.app 的数据')

    roots = _profile_roots()
    order = ['edge', 'chrome'] if not browser_pref else [browser_pref]
    for br in order:
        if br not in roots or not roots[br].is_dir():
            continue
        for name in _list_profiles(roots[br]):
            prof_dir = roots[br] / name
            if _has_lightnovel_indexeddb(prof_dir):
                return (br, roots[br], name)
    raise RuntimeError(
        '未找到含 lightnovel.app IndexedDB 的浏览器 profile。\n'
        '请先用该浏览器登录 https://www.lightnovel.app/，再运行本脚本。\n'
        '或用 --profile <路径> 显式指定。')


# ---- 磁盘读 IndexedDB（无浏览器）----

_ALNUM32 = re.compile(rb"[A-Za-z0-9]{32}")
_HEX32 = re.compile(rb"[0-9a-f]{32}")


def _read_token_from_disk(profile_dir):
    """Read RefreshToken straight from the profile's lightnovel IndexedDB leveldb.

    The token lives as a bare ASCII string in the leveldb log. Parsing the
    WriteBatch records is unreliable (Chromium compacts/recycles the .log, and the
    token can then sit outside a kFull record), so we scan the raw bytes instead.

    Discriminator: the refresh token is a 32-char alnum string that is NOT pure
    lowercase hex — the other 32-char values in this store are MD5 cache keys
    (pure `[0-9a-f]{32}`). No browser needed; returns the token or None.
    """
    idb_root = Path(profile_dir) / "IndexedDB"
    if not idb_root.is_dir():
        return None
    for leveldb_dir in idb_root.glob("*lightnovel*.indexeddb.leveldb"):
        for log_file in sorted(leveldb_dir.glob("*.log")):
            try:
                data = log_file.read_bytes()
            except Exception:
                continue
            for m in _ALNUM32.finditer(data):
                cand = m.group()
                if _HEX32.fullmatch(cand):
                    continue  # MD5 cache key, not the refresh token
                return cand.decode()
    return None


# ---- CDP 读 IndexedDB ----

def _read_indexeddb_token(ws):
    """在 lightnovel.app 页面上下文读 IndexedDB RefreshToken。

    USER_AUTHENTICATION store 存的是纯字符串记录（值即 token 字符串本身），
    非带 key 的对象。用 getAll 取所有记录取字符串。CDP awaitPromise 无法正确
    await 外层 return-new-Promise 里再 JSON.stringify 的嵌套形态，故让内层
    Promise 直接 resolve 数组、外层 await 后返回字符串，可稳定被 await。
    """
    js = f"""
    (async () => {{
        let db = await new Promise((r) => {{
            let q = indexedDB.open('{DB_NAME}');
            q.onsuccess = () => r(q.result);
            q.onerror = () => r(null);
        }});
        if (!db) return JSON.stringify({{ok:false, err:'open'}});
        let vals = [];
        try {{
            let tx = db.transaction('{STORE_NAME}', 'readonly');
            let store = tx.objectStore('{STORE_NAME}');
            let g = store.getAll();
            vals = await new Promise((r) => {{ g.onsuccess = () => r(g.result || []); g.onerror = () => r([]); }});
        }} catch (e) {{
            return JSON.stringify({{ok:false, err:String(e)}});
        }}
        vals = vals.filter(v => typeof v === 'string' && v);
        if (vals.length === 0) return JSON.stringify({{ok:false, err:'empty'}});
        return JSON.stringify({{ok:true, value: vals[0]}});
    }})()
    """
    raw = cdp_eval(ws, js, timeout=15, await_promise=True)
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {'ok': False, 'err': f'无法解析: {raw!r}'}
    if not data.get('ok'):
        err = data.get('err')
        if err == 'empty' or err is None:
            raise RuntimeError(
                'INDEXEDDB 里没有 RefreshToken（当前浏览器未登录）。\n'
                '请用该浏览器登录 https://www.lightnovel.app/，登录后再运行本脚本。')
        raise RuntimeError(f'读取 IndexedDB 失败: {err}')
    token = data.get('value')
    if not token or not isinstance(token, str):
        raise RuntimeError('未读到 RefreshToken（可能未登录）')
    return token


# ---- 写回 config.json ----

def update_config(token):
    """把 token 写回 config.json 的 lightnovel.refresh_token，保留其它字段。"""
    if not CONFIG_PATH.exists():
        raise RuntimeError(f'找不到 config.json: {CONFIG_PATH}')
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    data.setdefault('lightnovel', {})['refresh_token'] = token
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return CONFIG_PATH


# ---- 主流程 ----

def _exe_for(browser, profile_path, port):
    if browser == 'edge':
        exe = _edge_exe() or _chrome_exe()
    elif browser == 'chrome':
        exe = _chrome_exe() or _edge_exe()
    else:
        exe = _chrome_exe() or _edge_exe()
    if not exe:
        raise RuntimeError('找不到 Edge/Chrome 可执行文件')
    return exe


def main():
    parser = argparse.ArgumentParser(
        description='自动获取 lightnovel.app RefreshToken（从浏览器 IndexedDB）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
需要先登录 https://www.lightnovel.app/（用 Edge 或 Chrome）。
默认无浏览器直读磁盘 leveldb；读不到才回退 CDP（CDP 路径需先关闭该浏览器）。
  python refresh_token.py            # 磁盘读 → 写回 config.json
  python refresh_token.py --print    # 只打印 token 不写 config
  python refresh_token.py --profile <路径>
  python refresh_token.py --browser edge|chrome
        ''')
    parser.add_argument('--browser', choices=['edge', 'chrome'], help='强制指定浏览器（默认自动探测）')
    parser.add_argument('--profile', help='显式指定浏览器 User Data profile 路径')
    parser.add_argument('--print', action='store_true', help='只打印 token，不写 config.json')
    parser.add_argument('--port', type=int, default=9229, help='CDP 调试端口（默认 9229，避开 9222）')
    args = parser.parse_args()

    print('探测浏览器 profile...')
    browser, user_data_root, profile_name = detect_profile(args.browser, args.profile)
    print(f'  命中: {browser} profile = {user_data_root}\\{profile_name}')

    # 先试无浏览器直接读磁盘 leveldb（profile 被占用也能读）
    disk_token = _read_token_from_disk(user_data_root / profile_name)
    if disk_token:
        if args.print:
            print(f'\nRefreshToken（磁盘读取，未开浏览器）长度 {len(disk_token)}，未写入 config.json')
        else:
            path = update_config(disk_token)
            print(f'\n已更新: {path}')
            print(f'  lightnovel.refresh_token 已更新（磁盘读取，长度 {len(disk_token)}）')
        return 0

    # 磁盘读不到（token 落在 .ldb 或未登录）→ 回退浏览器 CDP 法
    if not args.print:
        print('  磁盘未读到 token，回退浏览器 CDP 读取...', file=sys.stderr)

    exe = _exe_for(browser, user_data_root, args.port)
    url = 'https://www.lightnovel.app/'
    proc = None
    ws = None
    try:
        # 启动浏览器（复用 profile，本次实例）。关脚本时只关本进程，不 kill 用户浏览器。
        # --user-data-dir 指向 User Data 根，profile 用 --profile-directory 指定。
        args_launch = [
            exe,
            f'--user-data-dir={user_data_root}',
            f'--profile-directory={profile_name}',
            f'--remote-debugging-port={args.port}',
            '--remote-allow-origins=*',
            '--new-window',
            '--no-first-run',
            '--window-position=-32000,-32000',
            '--window-size=1920,1080',
            url,
        ]
        print(f'启动浏览器 (port {args.port})...')
        proc = subprocess.Popen(args_launch,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        time.sleep(3)

        # 等页面加载 + CDP 就绪（复用 wait_for_page，但 CDP_HTTP 固定 9222，这里需连 9229）
        # 手动 wait
        import urllib.request, json as _j
        start = time.time()
        tab = None
        ws_url = None
        while time.time() - start < 60:
            try:
                tabsd = _j.loads(urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json', timeout=3).read())
                for t in tabsd:
                    if 'lightnovel.app' in t.get('url', '') and 'webSocketDebuggerUrl' in t:
                        tab = t
                        ws_url = t['webSocketDebuggerUrl']
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(2)
        if not ws_url:
            raise RuntimeError(
                '等待 lightnovel.app 页面加载超时。若浏览器报 "配置已损坏"，说明 profile 正被占用，'
                '请先关闭已打开的浏览器再运行。')

        ws = ws_connect(ws_url)
        time.sleep(2)  # 等 SPA 浅初始化

        print('读取 IndexedDB RefreshToken...')
        # SPA 可能异步写入 token，轮询等待 store 出现字符串（最多 ~40s）
        token = None
        last_err = None
        for attempt in range(20):
            try:
                token = _read_indexeddb_token(ws)
                break
            except RuntimeError as e:
                last_err = e
                time.sleep(2)
        if token is None:
            raise last_err

        if args.print:
            print(f'\nRefreshToken 读取成功（长度 {len(token)}，未写入 config.json）')
        else:
            path = update_config(token)
            print(f'\n已更新: {path}')
            print(f'  lightnovel.refresh_token 已更新（长度 {len(token)}）')
        return 0
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f'ERROR: {e}')
        sys.exit(1)
