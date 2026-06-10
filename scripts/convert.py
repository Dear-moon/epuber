#!/usr/bin/env python3
"""
TXT to EPUB converter for Chinese web novels.
Handles: encoding detection, chapter splitting, paragraph merging, EPUB3 generation.
Pure Python stdlib — no external dependencies required for core functionality.
"""

import re
import os
import sys
import io
import json
import zipfile
import argparse
import textwrap
from pathlib import Path
from datetime import datetime
from xml.etree import ElementTree as ET
from xml.dom import minidom

# Fix Windows terminal encoding for CJK output
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ============================================================
#  Encoding detection
# ============================================================

# Common Chinese encodings to try, in priority order
CJK_ENCODINGS = ['utf-8', 'gb18030', 'gbk', 'gb2312', 'big5', 'big5hkscs',
                 'shift_jis', 'euc_jp', 'utf-16', 'utf-16-le', 'utf-16-be']

def detect_encoding(filepath):
    """Detect the encoding of a text file by trying common CJK encodings.
    Returns (encoding, decoded_text)."""
    with open(filepath, 'rb') as f:
        raw = f.read()

    best_enc = 'utf-8'
    best_text = None
    best_score = -1

    for enc in CJK_ENCODINGS:
        try:
            text = raw.decode(enc)
            # Score: count CJK characters in first 20000 chars
            sample = text[:20000]
            cjk = sum(1 for c in sample if '一' <= c <= '鿿')
            japanese = sum(1 for c in sample if '぀' <= c <= 'ヿ')
            # Also check for common Chinese punctuation
            cn_punct = sum(1 for c in sample if c in '、。，；：“”‘’！？《》')
            replacements = sample.count('�')  # Unicode replacement char

            # A good CJK text has Chinese chars, low replacement chars
            if cjk > 50 and replacements < len(sample) * 0.01:  # <1% replacement
                score = cjk + japanese + cn_punct - replacements * 10
                if score > best_score:
                    best_score = score
                    best_enc = enc
                    best_text = text
        except (UnicodeDecodeError, UnicodeError):
            continue

    if best_text is not None:
        return best_enc, best_text

    # Fallback: try gb18030 with error replacement
    try:
        return 'gb18030', raw.decode('gb18030', errors='replace')
    except:
        return 'utf-8', raw.decode('utf-8', errors='replace')


# ============================================================
#  Chapter detection
# ============================================================

# Mapping Chinese numerals to integers
CN_NUMERALS = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '百': 100, '千': 1000, '万': 10000, '亿': 100000000,
}

def cn_to_int(s):
    """Convert Chinese numeral string to integer. E.g. '一百二十' → 120, '八十八' → 88."""
    s = s.strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)

    # Strategy: scan left to right, accumulating
    total = 0
    current = 0
    for ch in s:
        if ch not in CN_NUMERALS:
            return 0
        val = CN_NUMERALS[ch]
        if val >= 10:  # unit: 十, 百, 千, 万, 亿
            if current == 0:
                current = 1  # "十" alone means 10
            if val == 100000000:  # 亿
                total += current * val
                current = 0
            elif val == 10000:  # 万
                current *= val
                total += current
                current = 0
            else:
                current *= val
                total += current
                current = 0
        else:  # digit: 0-9
            current = val
    total += current
    return total

# Chapter regex patterns — ordered by specificity (most specific first)
CHAPTER_PATTERNS = [
    # 第X卷 第Y章 Title (分卷+章, e.g. "第一卷 第一章 开园")
    (r'^第([一二三四五六七八九十百千]+)卷\s+第([一二三四五六七八九十百千]+)章\s*(.*)$', 'volume_chapter'),
    # 第X卷 终章/尾声/后记/特典/插图 (volume-level special)
    (r'^第([一二三四五六七八九十百千]+)卷\s+(终章|最終話|尾声|後記|后记|特典|番外|插图|插畫|あとがき)\s*(.*)$', 'volume_special'),
    # 第X卷 Title (volume only, e.g. "第一卷 序")
    (r'^第([一二三四五六七八九十百千]+)卷\s+(.*)$', 'volume_title'),
    # 第X章 第Y章 Title (legacy 章章 format)
    (r'^第([一二三四五六七八九十百千]+)章\s+第([一二三四五六七八九十百千]+)章\s*(.*)$', 'volume_chapter'),
    # 第X章 Title (Chinese numerals + 章)
    (r'^第([一二三四五六七八九十百千万亿]+)章\s*(.*?)$', 'chapter'),
    # 第N章 Title (Arabic numerals + 章)
    (r'^第(\d+)\s*章\s*(.*?)$', 'chapter'),
    # Chapter N (English)
    (r'^(?:Chapter|CHAPTER)\s+(\d+)\s*(.*?)$', 'chapter'),
    # Special chapters
    (r'^(序章|楔子|序言|前言|引子)\s*(.*?)$', 'special'),
    (r'^(终章|尾声|后记|結尾|结局|大结局)\s*(.*?)$', 'special'),
    (r'^(番外|番外篇|外传|后日谈|幕间)\s*(.*?)$', 'special'),
    (r'^(?:Prologue|prologue|PROLOGUE)\s*(.*?)$', 'special'),
    (r'^(?:Epilogue|epilogue|EPILOGUE)\s*(.*?)$', 'special'),
]


def detect_chapter_format(lines):
    """Detect which chapter pattern(s) the file uses.
    Returns list of (line_index, chapter_type, chapter_number, title) tuples."""
    chapters = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern, ctype in CHAPTER_PATTERNS:
            m = re.match(pattern, stripped)
            if m:
                if ctype == 'volume_chapter':
                    vol_num = cn_to_int(m.group(1))
                    ch_num = cn_to_int(m.group(2))
                    title = m.group(3).strip()
                    chapters.append((i, 'volume_chapter', (vol_num, ch_num), title))
                elif ctype == 'volume_special':
                    vol_num = cn_to_int(m.group(1))
                    special_type = m.group(2)
                    title = m.group(3).strip()
                    chapters.append((i, 'volume_special', (vol_num, special_type), title))
                elif ctype == 'volume_title':
                    vol_num = cn_to_int(m.group(1))
                    title = m.group(2).strip()
                    chapters.append((i, 'volume_title', vol_num, title))
                elif ctype == 'chapter':
                    num_str = m.group(1)
                    if num_str.isdigit():
                        num = int(num_str)
                    else:
                        num = cn_to_int(num_str)
                    title = m.group(2).strip()
                    chapters.append((i, 'chapter', num, title))
                elif ctype == 'special':
                    ctype_str = m.group(1)
                    title = m.group(2).strip()
                    chapters.append((i, 'special', ctype_str, title))
                break
    return chapters


# ============================================================
#  Noise removal
# ============================================================

# Lines matching these patterns will be removed
NOISE_PATTERNS = [
    # Credits / metadata lines
    r'^(?:录入|录入者|扫图|扫图者|图源|製[作著]|整理|校对|排版|翻译|译者|译)\s*[：:].*$',
    # Source references
    r'^(?:更多.*|本书.*|内容.*)?(?:来自|来源|出自|转载|转自).*$',
    r'^本(?:书|文|站|作品|小説).*(?:来自|来源|出自|首发|发表于).*$',
    # Site watermarks
    r'^(?:www\.|http[s]?://|\[华夏\]|轻之国度|轻小说|轻之文库|天使动漫|动漫之家|精品小说|小說).*$',
    r'^\S*(?:小说库|文庫|文库|书馆|书馆|阅读|阅读网|文学城|小说网).*$',
    # Decorative separators
    r'^[※☆★◇◆□■△▲▽▼○●◎◉△▲▽▼◈◆◇◊]+\s*$',
    r'^[=＝\-－_＿#＃~～\*＊]{5,}\s*$',
    # Tips / ads
    r'^(?:温馨提示|小提示|tips|note|PS|P.S.|广告)[：:].*$',
    r'^(?:请|敬请|请您|歡迎|欢迎).*(?:支持|购买|收藏|订阅|打赏|点赞|投票|分享|转发|扩散).*$',
    r'^小说$|^全文$|^完本$|^全本$|^完结$|^TXT$|^txt$',
    # Page numbers / chapter references in text
    r'^\d+[/／]\d+\s*$',  # e.g., "1/233"
    # Standalone page numbers (3+ digits, optionally parenthesized)
    r'^[\(（]?\d{3,}[\)）]?\s*$',
]

def is_noise_line(line, line_index, total_lines):
    """Check if a line is noise that should be removed."""
    stripped = line.strip()
    if not stripped:
        return False  # Keep blank lines for paragraph detection

    # Remove lines matching noise patterns
    for pat in NOISE_PATTERNS:
        if re.match(pat, stripped, re.IGNORECASE):
            return True

    # Remove single-line time/date stamps (e.g. "2023-05-01 14:30")
    if re.match(r'^\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(:\d{2})?$', stripped):
        return True

    # Remove leading decorative symbols from lines that are otherwise normal
    # (keep them, just remove the symbol prefix later)

    return False


# ============================================================
#  Paragraph merging
# ============================================================

# Characters that typically end a sentence/paragraph in CJK text
SENTENCE_END_CJK = set('。！？…」』）)】》〉"\'\"、。')
SENTENCE_END_LATIN = set('.!?…\'"')

def is_sentence_end(char):
    """Check if a character marks the end of a sentence."""
    return char in SENTENCE_END_CJK or char in SENTENCE_END_LATIN

def merge_paragraphs(lines):
    """Merge hard-wrapped lines into proper paragraphs.

    Algorithm:
    - Consecutive non-blank lines are candidates for merging.
    - A line SHOULD merge with the next if it does NOT end with sentence-ending punctuation.
    - A line SHOULD NOT merge if it ends with sentence-ending punctuation AND the next line starts with a quotation mark.
    - Blank lines always separate paragraphs.
    - Lines that are clearly section headers (short, centered) are kept separate.
    """
    paragraphs = []
    current_para = []

    for line in lines:
        stripped = line.strip()

        # Blank line → flush current paragraph
        if not stripped:
            if current_para:
                paragraphs.append(''.join(current_para))
                current_para = []
            continue

        # Very short lines that look like headers
        if len(stripped) <= 20 and not current_para:
            paragraphs.append(stripped)
            continue

        if current_para:
            prev = current_para[-1]
            # Merge if previous line doesn't end with sentence-ending punctuation
            # OR if current line starts with dialogue continuation
            if prev and not is_sentence_end(prev[-1]):
                current_para.append(stripped)
            elif stripped and stripped[0] in '「『（(【[《"\'""':
                # Dialogue/quote continuation — merge with previous
                current_para.append(stripped)
            else:
                # Previous line was a sentence end, start new paragraph
                paragraphs.append(''.join(current_para))
                current_para = [stripped]
        else:
            current_para = [stripped]

    # Don't forget the last paragraph
    if current_para:
        paragraphs.append(''.join(current_para))

    return paragraphs


# ============================================================
#  Clean and structure
# ============================================================

def clean_text(text, volume_info=None):
    """Full text cleaning pipeline.
    Returns structured data: list of (chapter_type, chapter_num, title, paragraphs)."""

    lines = text.split('\n')
    total = len(lines)

    # Step 1: Remove noise lines
    clean_lines = []
    for i, line in enumerate(lines):
        if not is_noise_line(line, i, total):
            clean_lines.append(line)

    # Step 2: Detect chapters
    chapters_raw = detect_chapter_format(clean_lines)
    print(f"  Detected {len(chapters_raw)} chapter markers")

    # Step 3: Split by chapters and merge paragraphs within each chapter
    structured = []
    chapter_boundaries = [c[0] for c in chapters_raw]

    # Handle text before the first chapter marker (unnamed preface/prologue)
    if chapters_raw and chapters_raw[0][0] > 0:
        preface_lines = clean_lines[:chapters_raw[0][0]]
        # Check if there's meaningful text before the first chapter
        meaningful = [l for l in preface_lines if l.strip()]
        if len(meaningful) > 3:
            preface_paragraphs = merge_paragraphs(preface_lines)
            while preface_paragraphs and not preface_paragraphs[0].strip():
                preface_paragraphs.pop(0)
            while preface_paragraphs and not preface_paragraphs[-1].strip():
                preface_paragraphs.pop()
            if preface_paragraphs:
                structured.append({
                    'type': 'special',
                    'number': '序章',
                    'title': '',
                    'paragraphs': preface_paragraphs,
                })

    for idx, (line_idx, ctype, cnum, title) in enumerate(chapters_raw):
        # Determine end of this chapter
        next_idx = idx + 1
        if next_idx < len(chapter_boundaries):
            end_line = chapter_boundaries[next_idx]
        else:
            end_line = len(clean_lines)

        # Extract chapter body (skip the chapter marker line itself)
        body_lines = clean_lines[line_idx + 1:end_line]
        body_paragraphs = merge_paragraphs(body_lines)

        # Remove leading/trailing blank paragraphs
        while body_paragraphs and not body_paragraphs[0].strip():
            body_paragraphs.pop(0)
        while body_paragraphs and not body_paragraphs[-1].strip():
            body_paragraphs.pop()

        structured.append({
            'type': ctype,
            'number': cnum,
            'title': title,
            'paragraphs': body_paragraphs,
        })

    return structured


# ============================================================
#  EPUB generation (pure Python stdlib)
# ============================================================

CSS_STYLE = """
body {
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", serif;
  line-height: 1.8;
  margin: 0 0.5em;
  padding: 0;
  color: #333;
  background: #fff;
}
h1 {
  text-align: center;
  font-size: 1.6em;
  margin: 1.5em 0 1em;
  page-break-before: always;
}
h2 {
  text-align: center;
  font-size: 1.3em;
  margin: 1.2em 0 0.8em;
}
p {
  text-indent: 2em;
  margin: 0.3em 0;
}
p.no-indent {
  text-indent: 0;
}
.volume-title {
  text-align: center;
  font-size: 1.4em;
  font-weight: bold;
  margin: 1.5em 0;
  page-break-before: always;
}
.toc {
  margin: 1em 0;
}
.toc a {
  text-decoration: none;
  color: #333;
}
.toc li {
  list-style: none;
  margin: 0.3em 0;
}
"""


def make_epub(chapters_data, output_path, metadata):
    """Generate EPUB3 file from structured chapter data.

    Args:
        chapters_data: list of dicts {type, number, title, paragraphs}
        output_path: path for output .epub file
        metadata: dict with keys: title, author, language, description
    """
    # Try using ebooklib if available
    try:
        import ebooklib
        return _make_epub_ebooklib(chapters_data, output_path, metadata)
    except ImportError:
        pass

    return _make_epub_stdlib(chapters_data, output_path, metadata)


def _make_epub_stdlib(chapters_data, output_path, metadata):
    """Generate EPUB using only stdlib (zipfile + xml)."""

    book_id = 'novel-' + datetime.now().strftime('%Y%m%d%H%M%S')
    uid = f'{book_id}@claude-code'

    # Prepare EPUB structure
    # Get user's preferred language
    lang = 'zh-CN'

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:

        # --- mimetype (must be first, uncompressed) ---
        zf.writestr('mimetype', 'application/epub+zip', zipfile.ZIP_STORED)

        # --- META-INF/container.xml ---
        container_xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
          <rootfiles>
            <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
          </rootfiles>
        </container>
        """)
        zf.writestr('META-INF/container.xml', container_xml)

        # --- Generate chapter XHTML files ---
        manifest_items = []
        spine_items = []
        nav_items = []

        for idx, ch in enumerate(chapters_data):
            ch_id = f'chapter-{idx + 1}'
            file_name = f'{ch_id}.xhtml'
            manifest_items.append((ch_id, file_name, 'application/xhtml+xml'))
            spine_items.append(ch_id)

            # Build chapter title
            if ch['type'] == 'volume_chapter':
                vol_num, ch_num = ch['number']
                display = f'第{ch_num}章'
                if ch['title']:
                    display += f' {ch["title"]}'
                nav_items.append((ch_id, display, idx + 1))
            elif ch['type'] == 'volume_title':
                vol_num = ch['number']
                display = ch['title'] if ch['title'] else f'第{vol_num}卷'
                nav_items.append((ch_id, display, idx + 1))
            elif ch['type'] == 'volume_special':
                vol_num, special_type = ch['number']
                display = special_type if not ch['title'] else f'{special_type}: {ch["title"]}'
                nav_items.append((ch_id, display, idx + 1))
            elif ch['type'] == 'special':
                display = ch['number'] if not ch['title'] else f'{ch["number"]}: {ch["title"]}'
                nav_items.append((ch_id, display, idx + 1))
            else:
                display = f'第{ch["number"]}章'
                if ch['title']:
                    display += f' {ch["title"]}'
                nav_items.append((ch_id, display, idx + 1))

            # Build chapter body
            body_parts = [f'<h1>{_escape_xml(display)}</h1>']
            for p in ch['paragraphs']:
                p = p.strip()
                if not p:
                    continue
                body_parts.append(f'<p>{_escape_xml(p)}</p>')

            xhtml = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE html>
            <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}">
            <head>
              <title>{_escape_xml(display)}</title>
              <link rel="stylesheet" type="text/css" href="style.css"/>
            </head>
            <body>
              {"".join(body_parts)}
            </body>
            </html>
            """)
            zf.writestr(f'OEBPS/{file_name}', xhtml)

        # --- style.css ---
        zf.writestr('OEBPS/style.css', CSS_STYLE)

        # --- toc.ncx ---
        ncx_parts = []
        for idx, (ch_id, display, play_order) in enumerate(nav_items):
            ncx_parts.append(textwrap.dedent(f"""\
            <navPoint id="nav-{idx + 1}" playOrder="{play_order}">
              <navLabel><text>{_escape_xml(display)}</text></navLabel>
              <content src="{ch_id}.xhtml"/>
            </navPoint>
            """))

        toc_ncx = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE ncx PUBLIC "-//IDPF//DTD NXC 1.1//EN" "http://www.idpf.org/dtds/ncx-1.1.dtd">
        <ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
          <head>
            <meta name="dtb:uid" content="{uid}"/>
            <meta name="dtb:depth" content="1"/>
            <meta name="dtb:totalPageCount" content="0"/>
            <meta name="dtb:maxPageNumber" content="0"/>
          </head>
          <docTitle><text>{_escape_xml(metadata.get('title', 'Untitled'))}</text></docTitle>
          <navMap>
            {"".join(ncx_parts)}
          </navMap>
        </ncx>
        """)
        zf.writestr('OEBPS/toc.ncx', toc_ncx)

        # --- content.opf ---
        manifest_xml_parts = []
        for ch_id, file_name, media_type in manifest_items:
            manifest_xml_parts.append(
                f'<item id="{ch_id}" href="{file_name}" media-type="{media_type}"/>'
            )

        # Additional manifest items
        manifest_xml_parts.append('<item id="style" href="style.css" media-type="text/css"/>')
        manifest_xml_parts.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')

        spine_xml_parts = [f'<itemref idref="{ch_id}"/>' for ch_id in spine_items]

        title = _escape_xml(metadata.get('title', 'Untitled'))
        author = _escape_xml(metadata.get('author', 'Unknown'))
        description = _escape_xml(metadata.get('description', ''))

        content_opf = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang="{lang}">
          <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
            <dc:identifier id="book-id">{uid}</dc:identifier>
            <dc:title>{title}</dc:title>
            <dc:creator>{author}</dc:creator>
            <dc:language>{lang}</dc:language>
            <dc:description>{description}</dc:description>
            <meta property="dcterms:modified">{datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')}</meta>
          </metadata>
          <manifest>
            {"".join(manifest_xml_parts)}
          </manifest>
          <spine toc="ncx">
            {"".join(spine_xml_parts)}
          </spine>
        </package>
        """)
        zf.writestr('OEBPS/content.opf', content_opf)

    print(f"  EPUB written to: {output_path}")
    return output_path


def _make_epub_ebooklib(chapters_data, output_path, metadata):
    """Generate EPUB using ebooklib (richer features)."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier(f'novel-{datetime.now().strftime("%Y%m%d%H%M%S")}@claude-code')
    book.set_title(metadata.get('title', 'Untitled'))
    book.set_language(metadata.get('language', 'zh-CN'))
    book.add_author(metadata.get('author', 'Unknown'))
    if metadata.get('description'):
        book.add_metadata('DC', 'description', metadata['description'])

    # Default CSS
    style = CSS_STYLE
    nav_css = epub.EpubItem(uid="style", file_name="style.css",
                            media_type="text/css", content=style.encode('utf-8'))
    book.add_item(nav_css)

    # Build chapters
    epub_chapters = []
    for idx, ch in enumerate(chapters_data):
        # Build display title
        if ch['type'] == 'volume_chapter':
            vol_num, ch_num = ch['number']
            display = f'第{ch_num}章'
            if ch['title']:
                display += f' {ch["title"]}'
        elif ch['type'] == 'volume_title':
            vol_num = ch['number']
            display = ch['title'] if ch['title'] else f'第{vol_num}卷'
        elif ch['type'] == 'volume_special':
            vol_num, special_type = ch['number']
            display = special_type if not ch['title'] else f'{special_type}: {ch["title"]}'
        elif ch['type'] == 'special':
            display = ch['number'] if not ch['title'] else f'{ch["number"]}: {ch["title"]}'
        else:
            display = f'第{ch["number"]}章'
            if ch['title']:
                display += f' {ch["title"]}'

        # Build chapter content
        body = f'<h1>{_escape_xml(display)}</h1>\n'
        for p in ch['paragraphs']:
            p = p.strip()
            if p:
                body += f'<p>{_escape_xml(p)}</p>\n'

        ep_ch = epub.EpubHtml(title=display, file_name=f'chapter-{idx+1}.xhtml', lang='zh-CN')
        ep_ch.content = body.encode('utf-8')
        ep_ch.add_item(nav_css)
        book.add_item(ep_ch)
        epub_chapters.append(ep_ch)

    # Table of contents
    book.toc = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Spine
    book.spine = ['nav'] + epub_chapters

    epub.write_epub(output_path, book)
    print(f"  EPUB (ebooklib) written to: {output_path}")
    return output_path


# ============================================================
#  Helpers
# ============================================================

def _escape_xml(text):
    """Escape text for XML/HTML."""
    if not text:
        return ''
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = text.replace('"', '&quot;')
    text = text.replace("'", '&apos;')
    return text


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='TXT to EPUB converter for Chinese web novels',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          python convert.py novel.txt
          python convert.py novel.txt -o output.epub --title "My Novel" --author "Author"
          python convert.py file1.txt file2.txt -o merged.epub
        """))
    parser.add_argument('files', nargs='*', help='Input TXT file(s)')
    parser.add_argument('-o', '--output', help='Output EPUB file path')
    parser.add_argument('--title', help='Book title (auto-detected if not specified)')
    parser.add_argument('--author', default='Unknown', help='Author name')
    parser.add_argument('--language', default='zh-CN', help='Book language')
    parser.add_argument('--url', help='Fetch from URL instead of local file')
    parser.add_argument('--description', default='', help='Book description')

    args = parser.parse_args()

    if not args.files and not args.url:
        parser.error('No input files or --url specified')

    if args.url:
        print(f"Fetching from URL: {args.url}")
        try:
            from web_fetch import fetch_novel
            text, meta = fetch_novel(args.url)
            if not args.title:
                args.title = meta.get('title', 'Untitled')
            if not args.author or args.author == 'Unknown':
                args.author = meta.get('author', 'Unknown')
        except ImportError:
            print("Error: web_fetch.py not found. Install requests + beautifulsoup4 for web fetching.")
            sys.exit(1)
        all_texts = [text]
    else:
        all_texts = []
        for fpath in args.files:
            print(f"Processing: {fpath}")
            enc, text = detect_encoding(fpath)
            print(f"  Encoding: {enc}, {len(text)} chars")
            all_texts.append(text)
        text = '\n\n'.join(all_texts)

    # Auto-detect title from filename if not provided
    if not args.title:
        if args.files:
            name = Path(args.files[0]).stem
            # Clean up common suffixes
            name = re.sub(r'[（(].*?完.*?[）)]', '', name)
            name = re.sub(r'[（(].*?全.*?[）)]', '', name)
            name = name.strip()
            args.title = name
        else:
            args.title = 'Untitled'

    # Determine output path
    if not args.output:
        base = args.title if args.title != 'Untitled' else 'output'
        args.output = f'{base}.epub'

    # Clean and structure
    print(f"  Title: {args.title}")
    print(f"  Author: {args.author}")
    chapters = clean_text(text)
    print(f"  Chapters: {len(chapters)}")

    if not chapters:
        print("Warning: No chapters detected. Treating entire text as one chapter.")
        # Split text into paragraphs as one big chapter
        lines = text.split('\n')
        clean_lines = [l for i, l in enumerate(lines) if not is_noise_line(l, i, len(lines))]
        paragraphs = merge_paragraphs(clean_lines)
        chapters = [{
            'type': 'chapter',
            'number': 1,
            'title': '',
            'paragraphs': paragraphs,
        }]

    # Generate EPUB
    metadata = {
        'title': args.title,
        'author': args.author,
        'language': args.language,
        'description': args.description,
    }
    make_epub(chapters, args.output, metadata)
    print(f"Done: {args.output}")


if __name__ == '__main__':
    main()
