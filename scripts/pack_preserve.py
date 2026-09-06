#!/usr/bin/env python3
"""
Layout-preserving TXT -> EPUB packer.

Unlike convert.py (which noise-cleans and merges hard-wrapped lines for raw
web novels), this keeps an already-typeset TXT intact:
  - one non-empty source line -> one <p> (paragraph boundaries preserved)
  - a blank source line -> a scene-break gap (kept, not merged away)
  - chapter titles are split on a configurable regex (default matches common
    headers like 序章/第X話/終章 and 特典SS titles)
Optional --convert {t2s,s2t} applies OpenCC + a 著->着 aspect-particle fix.

Usage:
  python pack_preserve.py book.txt -o book.epub --title "標題" --author "作者" \
      --convert t2s --cover cover.jpg --images-dir imgdir
"""

import sys, re, html, zipfile, argparse
from pathlib import Path

# Aspect particle 著(着) must become 着 in Simplified; 著 in zhù-compounds stays.
_ZHU = ['著作', '著名', '名著', '原著', '编著', '专著', '巨著', '译著', '遗著',
        '著述', '著者', '著录', '显著', '卓著', '土著', '昭著']
_ZHU_RESTORE = sorted([(w.replace('著', '着'), w) for w in _ZHU], key=lambda x: -len(x[0]))

# Default chapter regex: matches common standalone headers at line start.
# Override with --chapter-regex for books with unusual headers.
_DEFAULT_CHAPTER_RE = (
    r'^(第[0-9一二三四五六七八九十百千零]+[章节話话节節回卷部]'
    r'|序章|終章|终章|番外|外传|外傳|幕間|後日談|后日谈|间章|間章|楔子|前传|前傳'
    r'|.*特典SS|【.*SS】|.*番外篇)'
)


def esc(s):
    return html.escape(s, quote=True)


def make_converter(mode):
    """str->str converter; identity for none."""
    if not mode or mode == 'none':
        return lambda s: s
    try:
        import opencc
    except ImportError:
        sys.stderr.write('ERROR: --convert requires opencc-python-reimplemented. pip install opencc-python-reimplemented\n')
        sys.exit(2)
    cc = opencc.OpenCC(mode)

    def _t2s(t):
        t = cc.convert(t)
        t = t.replace('著', '着')          # aspect-particle 著 -> 着
        for corrupted, intact in _ZHU_RESTORE:  # restore zhù compounds
            if corrupted != intact:
                t = t.replace(corrupted, intact)
        return t
    return _t2s if mode == 't2s' else cc.convert


def detect_chapters(lines, chapter_re):
    """Return [(title, start_line)] of chapter headers in order.

    Many typeset TXTs include an inline TOC that repeats every chapter title once
    before the body. For each distinct title keep only its LAST occurrence (the
    body header); the earlier TOC duplicate is dropped. A title appearing once is
    kept as-is.
    """
    rx = re.compile(chapter_re)
    matches = []
    for i, l in enumerate(lines):
        s = l.strip()
        if s and rx.match(s):
            matches.append((s, i))
    last = {}
    for s, i in matches:
        last[s] = i
    out = sorted(last.items(), key=lambda x: x[1])
    return [(s, i) for s, i in out]


def metadata_from(text, title, author):
    """Pull title/author from header if not given (like '中文標題：xxx').

    title is searched top-down: a 中文標題/标题 line is authoritative, then a
    bare 標題/标题, then a 书名/書名 fallback. Order matters: '日文书名：…' also
    contains 书名 but is the JAPANESE title, so it must only be used as a fallback.
    """
    if not title:
        for pat in (r'中文標題[：:]\s*(.+)', r'中文标题[：:]\s*(.+)',
                    r'標題[（(]?中文[)）]?[：:]\s*(.+)', r'標題[：:]\s*(.+)',
                    r'标题[：:]\s*(.+)', r'[书書]名[：:]\s*(.+)'):
            m = re.search(pat, text)
            if m:
                title = m.group(1).strip()
                break
    if not author:
        m = re.search(r'(?:作者|著者)[：:]\s*(.+)', text)
        if m:
            author = m.group(1).strip()
    return title or '未命名', author or '佚名'


def build(text_path, output, title, author, convert, convert_mode, chapter_re,
          cover, images_dir, scene_break, lang):
    text = Path(text_path).read_text(encoding='utf-8-sig')
    lines = text.split('\n')
    title, author = metadata_from(text, title, author)

    chapters = detect_chapters(lines, chapter_re)
    print(f'detected {len(chapters)} chapter headers')

    # Drop leading front matter (title/TOC block) when header-like; keep it only if it
    # holds real body text. Header-like = contains title/author markers or a lined TOC.
    structured = []
    if chapters and chapters[0][1] > 0:
        front = lines[:chapters[0][1]]
        looks_meta = any(re.search(r'(書名|书名|標題|标题|作者|著者|日文)[：:]', l) for l in front)
        if looks_meta:
            print(f'dropped {chapters[0][1]} front-matter line(s)')
        else:
            paras = render_paragraphs('\n'.join(front), convert, scene_break)
            if any(p.strip() for p in paras):
                structured.append(('前言', paras))

    for idx, (t, start) in enumerate(chapters):
        end = chapters[idx + 1][1] if idx + 1 < len(chapters) else len(lines)
        chunk = '\n'.join(lines[start + 1:end])
        structured.append((t, render_paragraphs(chunk, convert, scene_break)))

    if not structured:
        # No chapter headers found: treat whole text as one chapter.
        structured.append((title, render_paragraphs(text, convert, scene_break)))

    images = {}
    if images_dir and Path(images_dir).is_dir():
        for p in sorted(Path(images_dir).iterdir()):
            if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
                images[p.name] = p.read_bytes()
    if cover:
        cp = Path(cover)
        if cp.is_file() and cp.name not in images:
            images[cp.name] = cp.read_bytes()
    cover_name = Path(cover).name if cover and Path(cover).is_file() else None
    appendix = sorted(n for n in images if n != cover_name)
    print(f'images={len(images)} cover={cover_name} appendix={len(appendix)}')

    if not lang:
        lang = 'zh-CN' if convert_mode == 't2s' else 'zh-TW'

    out = Path(output)
    uid = 'urn:preserve-' + re.sub(r'[^A-Za-z0-9]', '-', title)[:40] + str(len(structured))

    css = ('''body { max-width: 800px; margin: 0 auto; padding: 1.5em 1em; line-height: 1.9; font-size: 1.05em; }
p { margin: 0.5em 0; text-align: justify; }
p.scene-break { margin: 1.5em 0; height: 0.6em; }
h1.chap { text-align: center; font-size: 1.4em; margin: 1.2em 0; }
.cover { text-align: center; }
.cover img { max-width: 100%; }
.illus { text-align: center; margin: 1.2em 0; }
.illus img { max-width: 100%; }
.illus figcaption { font-size: 0.85em; color: #666; }
''')

    manifest, spine, ncx_items, navpoints = [], [], [], []
    manifest.append('<item id="css" href="css/style.css" media-type="text/css"/>')
    chapter_xhtml = {}

    if cover_name:
        manifest.append(f'<item id="cover" href="images/{esc(cover_name)}" media-type="image/jpeg" properties="cover-image"/>')
        manifest.append('<item id="cover-page" href="text/cover.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="cover-page"/>')
        ncx_items.append('  <navPoint id="nav-cover" playOrder="1">\n    <navLabel><text>封面</text></navLabel>\n    <content src="text/cover.xhtml"/>\n  </navPoint>')
        navpoints.insert(0, '<li><a href="text/cover.xhtml">封面</a></li>')

    for i, (t, paras) in enumerate(structured):
        fid = f'ch{i:03d}'
        href = f'text/{fid}.xhtml'
        manifest.append(f'<item id="{fid}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{fid}"/>')
        ncx_items.append(f'  <navPoint id="nav{i}" playOrder="{i+2}">\n    <navLabel><text>{esc(convert(t))}</text></navLabel>\n    <content src="{href}"/>\n  </navPoint>')
        navpoints.append(f'<li><a href="{href}">{esc(convert(t))}</a></li>')
        body_html = f'<h1 class="chap">{esc(convert(t))}</h1>\n<div class="body">\n' + '\n'.join(paras) + '\n</div>\n'
        chapter_xhtml[fid] = _xhtml(convert(t), body_html)

    if appendix:
        aid = f'ch{len(structured):03d}'
        href = f'text/{aid}.xhtml'
        manifest.append(f'<item id="{aid}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{aid}"/>')
        ncx_items.append(f'  <navPoint id="nav{len(structured)}" playOrder="{len(structured)+2}">\n    <navLabel><text>插圖與特典</text></navLabel>\n    <content src="{href}"/>\n  </navPoint>')
        navpoints.append(f'<li><a href="{href}">插圖與特典</a></li>')
        for i, n in enumerate(appendix):
            manifest.append(f'<item id="img{i:03d}" href="images/{esc(n)}" media-type="image/jpeg"/>')
        figs = [f'<figure class="illus"><img src="../images/{esc(n)}" alt="{esc(n)}"/><figcaption>{esc(n)}</figcaption></figure>' for n in appendix]
        chapter_xhtml[aid] = _xhtml('插圖與特典', '<h1 class="chap">插圖與特典</h1>\n' + '\n'.join(figs) + '\n')

    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n<head>\n'
           f'<meta name="dtb:uid" content="{uid}"/>\n<meta name="dtb:depth" content="1"/>\n'
           '<meta name="dtb:totalPageCount" content="0"/>\n<meta name="dtb:maxPageNumber" content="0"/>\n</head>\n'
           f'<docTitle><text>{esc(convert(title))}</text></docTitle>\n<navMap>\n' + '\n'.join(ncx_items) + '\n</navMap>\n</ncx>\n')

    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">\n'
           '<head><meta charset="utf-8"/><title>目錄</title></head>\n'
           '<body><nav epub:type="toc"><h1>目錄</h1><ol>' + '\n'.join(navpoints) + '</ol></nav></body>\n</html>\n')

    opf = ('<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">\n'
           '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'    <dc:identifier id="book-id">{uid}</dc:identifier>\n'
           f'    <dc:title>{esc(convert(title))}</dc:title>\n'
           f'    <dc:creator>{esc(convert(author))}</dc:creator>\n'
           f'    <dc:language>{lang}</dc:language>\n'
           '    <meta property="dcterms:modified">2026-09-02T00:00:00Z</meta>\n'
           + (f'    <meta name="cover" content="cover"/>\n' if cover_name else '') +
           '  </metadata>\n'
           f'  <manifest>\n{chr(10).join(manifest)}\n  </manifest>\n  <spine>\n{chr(10).join(spine)}\n  </spine>\n</package>\n')

    with zipfile.ZipFile(str(out), 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        zf.writestr('META-INF/container.xml', '<?xml version="1.0" encoding="utf-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n')
        zf.writestr('OEBPS/content.opf', opf)
        zf.writestr('OEBPS/toc.ncx', ncx)
        zf.writestr('OEBPS/nav.xhtml', nav)
        zf.writestr('OEBPS/css/style.css', css)
        if cover_name:
            zf.writestr('OEBPS/text/cover.xhtml', _xhtml('封面', '<div class="cover"><img src="../images/%s" alt="cover"/></div>\n' % esc(cover_name)))
        for fid, x in chapter_xhtml.items():
            zf.writestr('OEBPS/text/' + fid + '.xhtml', x)
        for n, data in images.items():
            zf.writestr('OEBPS/images/' + n, data)

    print(f'\nEPUB saved: {out} ({out.stat().st_size/1024/1024:.1f} MB, {len(structured)}+{len(appendix)} chapters/images)')


def render_paragraphs(chunk, convert, scene_break):
    paras = []
    for line in chunk.split('\n'):
        s = line.strip()
        if s:
            paras.append(f'<p>{esc(convert(s))}</p>')
        elif scene_break:
            paras.append('<p class="scene-break">&nbsp;</p>')
    return paras


def _xhtml(title, body):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">\n'
            f'<head><meta charset="utf-8"/><title>{esc(title)}</title>\n'
            '<link rel="stylesheet" type="text/css" href="../css/style.css"/></head>\n'
            '<body>\n' + body + '\n</body>\n</html>\n')


def main():
    ap = argparse.ArgumentParser(description='Layout-preserving TXT -> EPUB')
    ap.add_argument('text', help='Input TXT file (UTF-8, may be BOM)')
    ap.add_argument('-o', '--output', default='output.epub', help='Output EPUB path')
    ap.add_argument('--title', default='', help='Book title (auto from header if omitted)')
    ap.add_argument('--author', default='', help='Author (auto from header if omitted)')
    ap.add_argument('--convert', default='none', choices=['none', 't2s', 's2t'],
                    help='OpenCC Simplified/Traditional conversion')
    ap.add_argument('--chapter-regex', default=_DEFAULT_CHAPTER_RE,
                    help='Regex to detect chapter title lines (default: common headers)')
    ap.add_argument('--cover', default='', help='Cover image path (embedded as EPUB cover)')
    ap.add_argument('--images-dir', default='', help='Directory of illustrations to append')
    ap.add_argument('--no-scene-break', action='store_true', help='Drop blank-line scene breaks')
    ap.add_argument('--lang', default='', help='DC:language (default: zh-CN if t2s else zh-TW}')
    args = ap.parse_args()
    build(args.text, args.output, args.title, args.author, make_converter(args.convert),
          args.convert, args.chapter_regex, args.cover, args.images_dir,
          not args.no_scene_break, args.lang)


if __name__ == '__main__':
    main()
