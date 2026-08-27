#!/usr/bin/env python3
"""
电子书工具集 — 统一入口。
Usage:
  python ebook.py lightnovel --bid <BID> --chapter <CID>            # 抓一章
  python ebook.py lightnovel --bid <BID> --chapter <CID> --html     # 生成 HTML
  python ebook.py lightnovel --bid <BID> --all                      # 抓全部 + 自动压制 EPUB
  python ebook.py lightnovel --bid <BID> --all --no-epub            # 只抓取不压制
  python ebook.py lightnovel --bid <BID> --pack-only                # 已有文件直接压制
  python ebook.py lightnovel --bid <BID> --pack-only --author "作者"
  python ebook.py syosetu -u "https://syosetu.org/novel/<ID>/"       # 抓取
  python ebook.py syosetu -u "..." --html <OUT_DIR>                  # HTML 模式
  python ebook.py novelia "https://n.novelia.cc/novel/<SOURCE>/<ID>"
  python ebook.py wenku "https://n.novelia.cc/wenku/<WID>"            # 文库版卷册 EPUB
  python ebook.py lk "https://www.lightnovel.fun/<LKID>"              # 轻之国度 (需 lk 账号)
  python ebook.py esj "https://www.esjzone.one/forum/<BOARD>/<ESJID>/"# esjzone (CDP 渲染)
  python ebook.py refresh-token                                # 自动获取轻国 RefreshToken
  python ebook.py masiro "https://masiro.me/admin/novelView?novel_id=<MSID>"  # 真白萌 (需 masiro 账号)
  python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"
  python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "书名"
  python ebook.py pack <BOOK_DIR> --author "作者"
  python ebook.py decode --snapshot page.html --font-url "https://..."
"""

import sys, os, subprocess, json, re, time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent / "scripts"

# Import config for shared settings
sys.path.insert(0, str(SCRIPTS))
try:
    from config import get_refresh_token as _get_refresh_token, get_fetch_dir
except ImportError:
    _get_refresh_token = lambda: None
    get_fetch_dir = lambda: Path.cwd() / "fetch"


def _run(script, *args):
    """Run a script in the scripts/ directory."""
    cmd = [sys.executable, str(SCRIPTS / script)] + list(args)
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


def _find_book_dir(bid):
    """Search fetch_dir for a book directory matching the given book_id."""
    fetch_dir = get_fetch_dir()
    for d in fetch_dir.iterdir():
        if not d.is_dir():
            continue
        info_path = d / 'book_info.json'
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding='utf-8'))
                if info.get('book_id') == bid:
                    return d
            except Exception:
                pass
    return None


def _route_web_to_wenku(url, epub_path):
    """If a web novel maps to a local wenku book dir, move its EPUB there as *web版*.

    wenku book dirs carry book_info.json with type='wenku' and web_ids
    (e.g. 'kakuyomu/16817330650648816745'). When the fetched web novel matches,
    the web EPUB is placed in the same dir to keep web + bunko versions together.

    Matching:
      1) 本地扫描: book_info 的 web_ids 精确/跨源匹配
      2) API 检测: web 小说元数据里的 wenkuId 命中本地 wenku 目录的 wid
      （若文库版存在但尚未本地下载，给出提示，不强行路由）
    """
    try:
        from web_fetch import parse_url, create_session, NOVELIA_API
    except Exception:
        return
    try:
        source, novel_id = parse_url(url)
    except Exception:
        return
    web_key = f'{source}/{novel_id}'

    fetch_dir = get_fetch_dir()
    ebook_root = fetch_dir.parent if fetch_dir.name == 'fetch' else fetch_dir
    src = Path(epub_path)
    if not src.exists():
        return str(src)

    # 通过 API 检测该 web 小说是否有关联的文库版（wenkuId）
    api_wid = None
    try:
        r = create_session().get(f'{NOVELIA_API}/novel/{source}/{novel_id}', timeout=15)
        if r.status_code == 200:
            api_wid = r.json().get('wenkuId')
    except Exception:
        api_wid = None

    # syosetu/narou/hameln 互备源共享同一小说 id，允许跨源匹配
    group = {'syosetu', 'narou', 'hameln'}

    for d in ebook_root.iterdir():
        if not d.is_dir():
            continue
        info_path = d / 'book_info.json'
        if not info_path.exists():
            continue
        try:
            info = json.loads(info_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if info.get('type') != 'wenku':
            continue

        ids = info.get('web_ids', [])
        matched = web_key in ids
        if not matched and api_wid and info.get('wid') == api_wid:
            matched = True
        if not matched and novel_id:
            for k in ids:
                if '/' not in k:
                    continue
                ks, kid = k.split('/', 1)
                if kid == novel_id and source in group and ks in group:
                    matched = True
                    break
        if not matched:
            continue

        target = d / f'{src.stem}_web版.epub'
        try:
            os.replace(str(src), str(target))
        except Exception as e:
            print(f'WARNING: could not move web EPUB to wenku dir: {e}')
            return str(src)
        info['web_version'] = {
            'source': source,
            'novel_id': novel_id,
            'file': target.name,
            'fetched_at': time.strftime('%Y-%m-%d %H:%M'),
        }
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Web version routed to wenku dir: {target}')
        return str(target)

    if api_wid:
        print(f'Note: this web novel has a bunko edition (wenku {api_wid}). '
              f'Run: python ebook.py wenku "https://n.novelia.cc/wenku/{api_wid}" '
              f'to download it — later web fetches will land in the same folder.')
    else:
        print('(no local wenku dir for this web novel; EPUB kept at default location)')
    return str(src)


def _pack_book(book_dir, author=None, output=None):
    """Pack a book directory into font-embedded EPUB."""
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', book_dir.name)
    if output is None:
        # Save to ebook root (parent of fetch dir), not inside fetch/
        ebook_root = book_dir.parent
        if ebook_root.name == 'fetch':
            ebook_root = ebook_root.parent
        output = str(ebook_root / f'{safe_name}.epub')
    print(f'Packing EPUB: {book_dir}')
    print(f'  Output: {output}')
    pack_args = [str(book_dir), '-o', output]
    if author:
        pack_args += ['--author', author]
    subprocess.run([sys.executable, str(SCRIPTS / 'html2epub_font.py')] + pack_args)
    print(f'EPUB saved: {output}')
    return output


def _record_lightnovel(bid, book_dir, epub_path):
    """记录 lightnovel 整书抓取事件到 fetch_records.json."""
    book_name = ''
    info_path = Path(book_dir) / 'book_info.json'
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding='utf-8'))
            book_name = info.get('book_name', '')
        except Exception:
            pass
    try:
        chapter_count = len(list(Path(book_dir).glob('*.html')))
    except Exception:
        chapter_count = 0
    try:
        from fetch_history import record as _record
        _record('lightnovel', book_name or f'bid {bid}', bid, chapter_count,
                'chapters', epub_path, 'app')
    except Exception:
        pass


def cmd_lightnovel(args):
    """lightnovel.app API 抓取 (Dart 桥接)"""
    # Extract flags and options
    force = '--force' in args
    pack_only = '--pack-only' in args
    no_epub = '--no-epub' in args

    author = None
    output_epub = None
    clean = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--author' and i + 1 < len(args):
            author = args[i + 1]; i += 1
        elif a in ('-o', '--output') and i + 1 < len(args):
            output_epub = args[i + 1]; i += 1
        elif a in ('--force', '--pack-only', '--no-epub'):
            pass
        else:
            clean.append(a)
        i += 1

    # --pack-only: 已有抓取文件，直接压制 EPUB
    if pack_only:
        bid = None
        for j, a in enumerate(clean):
            if a == '--bid' and j + 1 < len(clean):
                bid = int(clean[j + 1]); break
        if not bid:
            print("ERROR: --bid required for --pack-only"); sys.exit(1)

        book_dir = _find_book_dir(bid)
        if not book_dir:
            print(f"ERROR: No fetched data found for bid={bid}"); sys.exit(1)

        out = _pack_book(book_dir, author=author, output=output_epub)
        _record_lightnovel(bid, book_dir, out)
        return

    # --all: 抓取全部章节，默认生成 HTML 并自动压制 EPUB
    if '--all' in clean:
        bid = None
        for j, a in enumerate(clean):
            if a == '--bid' and j + 1 < len(clean):
                bid = int(clean[j + 1]); break
        if not bid:
            print("ERROR: --bid required for --all"); sys.exit(1)

        # Check memory
        from scripts.fetch_memory import FetchMemory
        mem = FetchMemory()
        fetched = mem.get_fetched_chapters("lightnovel", str(bid)) if not force else set()

        token = _get_refresh_token()
        if not token:
            print("ERROR: lightnovel.refresh_token not set in config.json"); sys.exit(1)

        import importlib.util
        spec = importlib.util.spec_from_file_location('api', str(SCRIPTS / 'lightnovel_api.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        data = mod._fetch_chapter_via_dart(token, bid, 1)
        total = len(data['Chapter'].get('Chapters', [])) or 1
        book_name = data['Chapter'].get('BookName', '')

        if fetched:
            print(f"Memory: {len(fetched)}/{total} already fetched. Use --force to re-fetch all.")

        # --all implies --html for font-embedded EPUB output
        extra = [a for a in clean if a != '--all']
        if '--html' not in extra:
            extra.append('--html')

        for ch in range(1, total + 1):
            if ch in fetched:
                print(f"[{ch}/{total}] (skip)", flush=True)
                continue
            print(f"[{ch}/{total}]", end=' ', flush=True)
            subprocess.run([sys.executable, str(SCRIPTS / 'lightnovel_api.py'),
                           '--bid', str(bid), '--chapter', str(ch)] + extra)
            # Memory is recorded by lightnovel_api.py itself

        # Auto-pack EPUB (unless --no-epub)
        if not no_epub:
            print(f"\n{'='*50}")
            book_dir = _find_book_dir(bid)
            if book_dir:
                out = _pack_book(book_dir, author=author, output=output_epub)
                _record_lightnovel(bid, book_dir, out)
            else:
                print(f"WARNING: Could not find book directory for bid={bid}, skip packing")
        return

    _run('lightnovel_api.py', *clean)


def cmd_syosetu(args):
    """syosetu.org CDP 抓取"""
    # Extract novel ID to track
    novel_id = None
    for i, a in enumerate(args):
        if a in ('-u', '--url') and i + 1 < len(args):
            m = re.search(r'/novel/(\d+)', args[i + 1])
            if m: novel_id = m.group(1)
            break
    force = '--force' in args
    filtered = [a for a in args if a != '--force']
    _run('syosetu_fetch.py', *filtered)
    # Memory is recorded by syosetu_fetch.py itself


def cmd_novelia(args):
    """novelia.cc API 抓取 → 自动转 EPUB + 清理临时 TXT"""
    force = '--force' in args
    no_epub = '--no-epub' in args
    jp_mode = '--jp' in args
    bilingual = '-b' in args or '--bilingual' in args

    # Extract -o, -t, -w, -d flags
    output_epub = None
    translation = None
    workers = None
    delay = None
    clean = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('-o', '--output') and i + 1 < len(args):
            output_epub = args[i + 1]; i += 1
        elif a in ('-t', '--translation') and i + 1 < len(args):
            translation = args[i + 1]; i += 1
        elif a in ('-w', '--workers') and i + 1 < len(args):
            workers = args[i + 1]; i += 1
        elif a in ('-d', '--delay') and i + 1 < len(args):
            delay = args[i + 1]; i += 1
        elif a in ('--force', '--no-epub', '--jp', '-b', '--bilingual'):
            pass
        else:
            clean.append(a)
        i += 1

    if not clean:
        print("ERROR: URL required"); sys.exit(1)

    url = clean[0]

    # Fetch to temp TXT
    import tempfile
    fetch_dir = get_fetch_dir()
    tmp_txt = str(fetch_dir / f'_fetch_{os.getpid()}.txt')

    fetch_args = [url, '-o', tmp_txt]
    if translation:
        fetch_args += ['-t', translation]
    if workers:
        fetch_args += ['-w', workers]
    if delay:
        fetch_args += ['-d', delay]
    if jp_mode:
        fetch_args += ['--jp']
    if bilingual:
        fetch_args += ['--bilingual']

    print(f"Fetching: {url}")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / 'web_fetch.py')] + fetch_args
    )
    if result.returncode != 0:
        print("Fetch failed"); sys.exit(result.returncode)

    if not os.path.exists(tmp_txt):
        print("ERROR: TXT not generated"); sys.exit(1)

    # Parse metadata from web_fetch.py output for author/title
    # We read the TXT header to get title and author (also count chapters for timeline record)
    with open(tmp_txt, 'r', encoding='utf-8') as f:
        txt_all = f.read()
    header = txt_all[:500]
    title_match = re.search(r'^# (.+)', header, re.MULTILINE)
    author_match = re.search(r'作者: (.+)', header)
    title = title_match.group(1).strip() if title_match else 'Untitled'
    author = author_match.group(1).strip() if author_match else ''
    chapter_count = len(re.findall(r'第\d+章', txt_all))

    if no_epub:
        # Just keep the TXT where user wants it
        if output_epub:
            import shutil
            shutil.move(tmp_txt, output_epub)
            print(f"TXT saved: {output_epub}")
        else:
            print(f"TXT saved: {tmp_txt}")
        return

    # Auto-convert to EPUB
    safe = re.sub(r'[<>:\"/\\|?*]', '_', title)[:80]
    if output_epub is None:
        epub_path = str(fetch_dir.parent / f'{safe}.epub')
    else:
        epub_path = output_epub

    author_flag = ['--author', author] if author else []
    subprocess.run(
        [sys.executable, str(SCRIPTS / 'convert.py'), tmp_txt, '-o', epub_path,
         '--title', title] + author_flag
    )

    # Clean up temp TXT
    try:
        os.remove(tmp_txt)
    except Exception:
        pass

    if os.path.exists(epub_path):
        size_mb = os.path.getsize(epub_path) / (1024*1024)
        print(f"EPUB: {size_mb:.1f} MB → {epub_path}")
        # 若该 web 小说对应某本地文库版目录，把 web 版挪进同一目录
        final_path = _route_web_to_wenku(url, epub_path) or epub_path
        # 时间轴抓取记录
        try:
            from fetch_history import record as _record
            from web_fetch import parse_url as _parse_url
            _src, _nid = _parse_url(url)
            _record('novelia', title, f'{_src}/{_nid}', chapter_count,
                    'chapters', final_path, 'web')
        except Exception:
            pass
    else:
        print(f"ERROR: EPUB not created")


def cmd_wenku(args):
    """novelia 文库版下载 (每本书一目录，含卷册 EPUB)"""
    _run('wenku_fetch.py', *args)


def cmd_lk(args):
    """轻之国度 (lightnovel.fun) 抓取 → 自动转 EPUB"""
    _run('lk_fetch.py', *args)


def cmd_esj(args):
    """esjzone (www.esjzone.one) 抓取 → 自动转 EPUB"""
    _run('esj_fetch.py', *args)


def cmd_refresh_token(args):
    """自动获取 lightnovel.app RefreshToken (从浏览器 IndexedDB)"""
    _run('refresh_token.py', *args)


def cmd_masiro(args):
    """真白萌 (masiro.me) 抓取 → 自动转 EPUB"""
    _run('masiro_fetch.py', *args)


def cmd_wenku8(args):
    """wenku8.net CDP 抓取"""
    novel_id = None
    for i, a in enumerate(args):
        if a in ('-u', '--url') and i + 1 < len(args):
            m = re.search(r'/novel/\d+/(\d+)', args[i + 1])
            if m: novel_id = m.group(1)
            break
    force = '--force' in args
    filtered = [a for a in args if a != '--force']
    _run('wenku8_fetch.py', *filtered)
    # Memory is recorded by wenku8_fetch.py itself


def cmd_convert(args):
    """TXT → EPUB"""
    _run('convert.py', *args)


def cmd_pack(args):
    """HTML 目录 → 字体嵌入 EPUB (WOFF2→TTF + 插图)"""
    _run('html2epub_font.py', *args)


def cmd_decode(args):
    """字体解码（离线快照）"""
    _run('lightnovel_decode.py', *args)


def cmd_memory(args):
    """管理抓取记忆"""
    import importlib.util
    spec = importlib.util.spec_from_file_location('fetch_memory', str(SCRIPTS / 'fetch_memory.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cmd_memory(args)


def cmd_ocr(args):
    """[实验] VLM OCR 字形映射表"""
    _run('build_decode_map.py', *args)


def print_help():
    print(__doc__)
    print("子命令:")
    print("  lightnovel     lightnovel.app API 抓取 (Dart 桥接)")
    print("                 --all 自动抓取全部章节 → 自动压制 EPUB")
    print("                 --pack-only 已有文件直接压制 EPUB")
    print("                 --no-epub 跳过自动压制")
    print("  syosetu        syosetu.org CDP 抓取 (Cloudflare 穿透)")
    print("  novelia        novelia.cc API 抓取")
    print("  wenku          novelia 文库版下载 (每本书一目录)")
    print("  lk             轻之国度 (lightnovel.fun) 抓取, 需 lk 账号")
    print("  esj            esjzone (www.esjzone.one) 抓取, CDP 渲染")
    print("  masiro         真白萌 (masiro.me) 抓取, 需 masiro 账号")
    print("  refresh-token  自动获取 lightnovel.app RefreshToken (浏览器 IndexedDB)")
    print("  wenku8         wenku8.net CDP 抓取")
    print("  convert        TXT → EPUB")
    print("  pack           HTML 目录 → 字体嵌入 EPUB")
    print("  decode         字体解码 (离线快照)")
    print("  ocr            [实验] VLM OCR 字形映射表")
    print("  memory         管理抓取记忆 (list/show/forget/clear)")
    print()
    print("通用选项:")
    print("  --force        忽略记忆，强制重新抓取")
    print("  --all          抓取全部章节 (自动跳过已抓取，默认生成 EPUB)")
    print("  --pack-only    跳过抓取，直接压制已有文件")
    print("  --no-epub      抓取但不自动压制 EPUB")
    print("  --author NAME  指定作者（嵌入 EPUB 元数据）")
    print("  -o, --output   指定 EPUB 输出路径")
    print()
    print("更多参数传给对应脚本，如:")
    print("  python ebook.py lightnovel --help")


COMMANDS = {
    'lightnovel': cmd_lightnovel,
    'syosetu': cmd_syosetu,
    'novelia': cmd_novelia,
    'wenku8': cmd_wenku8,
    'wenku': cmd_wenku,
    'lk': cmd_lk,
    'esj': cmd_esj,
    'masiro': cmd_masiro,
    'refresh-token': cmd_refresh_token,
    'convert': cmd_convert,
    'pack': cmd_pack,
    'decode': cmd_decode,
    'ocr': cmd_ocr,
    'memory': cmd_memory,
}


if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        print_help()
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"未知子命令: {cmd}\n")
        print_help()
        sys.exit(1)

    COMMANDS[cmd](sys.argv[2:])
