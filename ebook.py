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
  python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"
  python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "书名"
  python ebook.py pack <BOOK_DIR> --author "作者"
  python ebook.py decode --snapshot page.html --font-url "https://..."
"""

import sys, os, subprocess, json, re
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

        _pack_book(book_dir, author=author, output=output_epub)
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
                _pack_book(book_dir, author=author, output=output_epub)
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
    """novelia.cc API 抓取"""
    _run('web_fetch.py', *args)


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
