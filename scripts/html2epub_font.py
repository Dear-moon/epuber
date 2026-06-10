#!/usr/bin/env python3
"""
Pack font-obfuscated HTML chapters into a font-embedded EPUB with images.

Reads book_info.json (if present) for metadata auto-fill.
Images in images/ are embedded and src paths rewritten accordingly.

Usage:
  python html2epub_font.py "D:/.../Sword Art Online刀剑神域 Progressive 009" \
      -o "SAO_Progressive_009.epub" --author "川原礫"
"""

import sys, re, json, base64, argparse, zipfile, io
from pathlib import Path


def _woff2_to_ttf(woff2_bytes):
    """Convert WOFF2 font to TTF/OTF using fontTools. Returns (bytes, extension, mimetype)."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print("  WARNING: fonttools not installed, keeping WOFF2", file=sys.stderr)
        return woff2_bytes, 'woff2', 'font/woff2'

    buf = io.BytesIO(woff2_bytes)
    font = TTFont(buf)
    font.flavor = None  # Remove WOFF2 wrapper
    out = io.BytesIO()
    font.save(out)
    font.close()
    data = out.getvalue()

    # Detect format from saved font table
    if b'OTTO' in data[:4]:
        ext, mime = 'otf', 'font/opentype'
    else:
        ext, mime = 'ttf', 'font/truetype'
    return data, ext, mime


def _extract_font_and_body(html_text: str):
    m = re.search(r"base64,([A-Za-z0-9+/=]+)", html_text)
    font_bytes = base64.b64decode(m.group(1)) if m else None
    body_m = re.search(r"<body>(.*?)</body>", html_text, re.DOTALL)
    body = body_m.group(1) if body_m else html_text  # fallback: whole text is body
    return font_bytes, body.strip()


def _is_image_ext(name: str) -> bool:
    return name.rsplit('.', 1)[-1].lower() in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp')


_MIME_MAP = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp', 'svg': 'image/svg+xml',
    'bmp': 'image/bmp',
}

_CSS_FONT = """@font-face {
  font-family: 'NovelFont';
  src: url('../font.__FEXT__') format('__FFMT__');
}
body {
  font-family: 'NovelFont', serif;
  max-width: 800px;
  margin: 0 auto;
  padding: 1.5em 1em;
  line-height: 1.9;
  font-size: 18px;
}
p { text-indent: 1em; margin: 0.3em 0; }
.biaoti1 { font-size: 1.4em; font-weight: bold; text-align: center; margin: 1em 0; text-indent: 0; }
.biaoti2 { text-align: center; margin: 0.5em 0; font-size: 0.95em; text-indent: 0; }
.empty-line { height: 1em; text-indent: 0; }
.ruby { display: inline; }
img { max-width: 100%; height: auto; }
.illus img { display: block; margin: 1em auto; }
.cover { text-align: center; text-indent: 0; }
"""

_CSS_PLAIN = """body {
  font-family: "Microsoft YaHei", "SimSun", "Noto Serif CJK SC", "Yu Mincho", serif;
  max-width: 800px;
  margin: 0 auto;
  padding: 1.5em 1em;
  line-height: 1.9;
  font-size: 18px;
}
p { margin: 0.5em 0; text-indent: 1em; }
h1 { text-align: center; font-size: 1.4em; text-indent: 0; }
.biaoti1 { font-size: 1.4em; font-weight: bold; text-align: center; margin: 1em 0; text-indent: 0; }
.biaoti2 { text-align: center; margin: 0.5em 0; font-size: 0.95em; text-indent: 0; }
.empty-line { height: 1em; text-indent: 0; }
img { max-width: 100%; height: auto; display: block; margin: 1em auto; }
"""

_CHAPTER_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="../css/style.css"/>
</head>
<body>
{body}
</body>
</html>
"""

_CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_NCX_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head>
  <meta name="dtb:uid" content="{uid}"/>
  <meta name="dtb:depth" content="1"/>
  <meta name="dtb:totalPageCount" content="0"/>
  <meta name="dtb:maxPageNumber" content="0"/>
</head>
<docTitle><text>{title}</text></docTitle>
<navMap>
"""

_NCX_ITEM = """  <navPoint id="nav{i}" playOrder="{i}">
    <navLabel><text>{title}</text></navLabel>
    <content src="chapters/ch{i:04d}.xhtml"/>
  </navPoint>
"""

_NCX_TAIL = """</navMap>
</ncx>
"""


def build_epub(html_dir: str, output: str, title: str, author: str):
    root = Path(html_dir)
    html_files = sorted(root.glob("*.html"))
    if not html_files:
        print("ERROR: No HTML files found", file=sys.stderr)
        sys.exit(1)

    # Load book_info.json if present
    info_path = root / "book_info.json"
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding='utf-8'))
        if not title or title == 'Unknown':
            title = info.get('book_name', title)
        if not author or author == 'Unknown':
            author = info.get('author', author)
        cover_file = info.get('cover', '')
    else:
        cover_file = ''

    # Collect images
    img_dir = root / "images"
    image_files = {}  # filename → bytes
    if img_dir.is_dir():
        for p in sorted(img_dir.iterdir()):
            if p.is_file() and _is_image_ext(p.name):
                image_files[p.name] = p.read_bytes()
                print(f"  Image: {p.name} ({len(image_files[p.name]):,} bytes)", file=sys.stderr)

    # Detect cover
    cover_id = None
    if cover_file and cover_file in image_files:
        cover_id = 'cover-img'
    elif image_files:
        # Try to find a cover-like image
        for fname in image_files:
            if 'cover' in fname.lower():
                cover_file = fname
                cover_id = 'cover-img'
                break

    print(f"Found {len(html_files)} HTML files, {len(image_files)} images", file=sys.stderr)

    # Read all chapters
    chapters = []
    font_bytes = None
    for p in html_files:
        text = p.read_text(encoding='utf-8')
        fb, body = _extract_font_and_body(text)
        if font_bytes is None:
            font_bytes = fb
        ch_title = p.stem
        # Rewrite image src from images/xxx.jpg to ../images/xxx.jpg (EPUB path)
        body = re.sub(r'src="images/', 'src="../images/', body)
        chapters.append((ch_title, body))
        print(f"  {p.name}: {len(body)} chars body", file=sys.stderr)

    has_font = font_bytes is not None
    font_ext = 'woff2'
    font_mime = 'font/woff2'
    font_format = 'woff2'
    if has_font:
        font_bytes, font_ext, font_mime = _woff2_to_ttf(font_bytes)
        font_format = 'truetype' if font_ext == 'ttf' else 'opentype'
        print(f"  Font converted: WOFF2 → {font_ext.upper()} ({len(font_bytes):,} bytes)", file=sys.stderr)
    else:
        print("  No embedded font found, using standard CSS", file=sys.stderr)

    uid = f"lightnovel-{hash(title)}-{len(chapters)}"

    # Build manifest / spine / NCX items
    manifest_items = []
    spine_items = []
    ncx_items = []

    # CSS + NCX
    manifest_items.append('<item id="css" href="css/style.css" media-type="text/css"/>')
    if has_font:
        manifest_items.append(f'<item id="font" href="font.{font_ext}" media-type="{font_mime}"/>')
    manifest_items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')

    # Images
    img_ids = {}
    for fname in image_files:
        ext = fname.rsplit('.', 1)[-1].lower()
        mime = _MIME_MAP.get(ext, 'application/octet-stream')
        img_id = f'img-{abs(hash(fname)) % 100000:05d}'
        img_ids[fname] = img_id
        props = ' properties="cover-image"' if fname == cover_file else ''
        manifest_items.append(
            f'    <item id="{img_id}" href="images/{fname}" media-type="{mime}"{props}/>'
        )

    # Chapters
    for i, (ch_title, body) in enumerate(chapters):
        fid = f"ch{i:04d}"
        manifest_items.append(
            f'    <item id="{fid}" href="chapters/{fid}.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'    <itemref idref="{fid}"/>')
        ncx_items.append(_NCX_ITEM.format(i=i, title=ch_title))

    opf = _build_opf(title, author, uid, manifest_items, spine_items, cover_id)
    ncx = (_NCX_HEAD.format(uid=uid, title=title)
           + "".join(ncx_items) + _NCX_TAIL)

    # Write EPUB
    output_path = Path(output)
    with zipfile.ZipFile(str(output_path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        if has_font:
            css = _CSS_FONT.replace('__FEXT__', font_ext).replace('__FFMT__', font_format)
        else:
            css = _CSS_PLAIN
        zf.writestr("OEBPS/css/style.css", css)
        if has_font:
            zf.writestr(f"OEBPS/font.{font_ext}", font_bytes)

        for fname, data in image_files.items():
            zf.writestr(f"OEBPS/images/{fname}", data)

        for i, (ch_title, body) in enumerate(chapters):
            xhtml = _CHAPTER_XHTML.format(title=ch_title, body=body)
            zf.writestr(f"OEBPS/chapters/ch{i:04d}.xhtml", xhtml)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\nEPUB saved: {output_path} ({size_mb:.1f} MB)", file=sys.stderr)


def _build_opf(title, author, uid, manifest_items, spine_items, cover_id):
    cover_meta = (f'\n    <meta name="cover" content="{cover_id}"/>') if cover_id else ''
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{uid}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>zh-CN</dc:language>{cover_meta}
    <meta property="dcterms:modified">{_now()}</meta>
  </metadata>
  <manifest>
{chr(10).join(manifest_items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine_items)}
  </spine>
</package>"""


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    parser = argparse.ArgumentParser(
        description="Pack font-obfuscated HTML chapters into a font-embedded EPUB")
    parser.add_argument("html_dir", help="Directory containing HTML files + images/ + book_info.json")
    parser.add_argument("-o", "--output", default="output.epub", help="Output EPUB path")
    parser.add_argument("--title", default="Unknown", help="Book title (auto-detect from book_info.json)")
    parser.add_argument("--author", default="Unknown", help="Author name (auto-detect from book_info.json)")
    args = parser.parse_args()

    build_epub(args.html_dir, args.output, args.title, args.author)


if __name__ == "__main__":
    main()
