# txt-to-epub 命令速查

## 快速开始

```bash
# 1. 解压 txt-to-epub-dist.zip 到任意目录
# 2. 双击运行 setup.bat (或终端执行)
setup.bat

# 3. 编辑 config.json, 填入你的 lightnovel.app refresh_token
#    获取方式: 浏览器打开 lightnovel.app → F12 → Application → Local Storage
#    复制 key: sb-yywiuxedvyfxdpznoyqy-auth-token 的值

# 4. 开始使用
python ebook.py lightnovel --bid 17028 --chapter 8 --html     # 单章
python ebook.py lightnovel --bid 17028 --all                   # 全部章节
```

## 快速使用

```bash
cd ~/.claude/skills/txt-to-epub

# 抓取 lightnovel.app
python ebook.py lightnovel --bid 17028 --chapter 8 --html     # 单章
python ebook.py lightnovel --bid 17028 --all                   # 全部章节

# 抓取 syosetu.org
python ebook.py syosetu -u "https://syosetu.org/novel/68239/" --html "dir"

# 抓取 novelia.cc
python ebook.py novelia "https://n.novelia.cc/novel/hameln/68239" -o novel.txt

# 抓取 wenku8.net
python ebook.py wenku8 "https://www.wenku8.net/novel/3/3988/index.htm" -o novel.txt

# TXT → EPUB
python ebook.py convert "novel.txt" -o "novel.epub" --title "书名" --author "作者"

# HTML 目录 → 字体嵌入 EPUB
python ebook.py pack "D:/.../Sword Art Online刀剑神域 Progressive 009" --author "川原礫"

# 字体解码（离线快照）
python ebook.py decode --html-file page.html --font-url "https://..."
```

## 安装

```bash
# 自动安装 (推荐)
setup.bat

# 手动安装
pip install -r requirements.txt

# lightnovel.app 抓取: 已预编译 bridge.exe，无需 Dart SDK
# 如需从源码重新编译 bridge:
#   winget install Google.DartSDK
#   cd scripts/dart_bridge && dart compile exe bin/lightnovel_bridge.dart -o bin/lightnovel_bridge.exe
```

## 配置

编辑 `config.json` (从 `config.example.json` 复制):
```json
{
  "lightnovel": {
    "refresh_token": "你的token"
  },
  "fetch_dir": "./fetch",           // 抓取输出目录
  "edge_path": null                 // null = 自动探测 Edge 路径
}
```

也可用环境变量覆盖: `LIGHTNOVEL_REFRESH_TOKEN`, `FETCH_DIR`, `EDGE_PATH`.

## 工作流概览

```
┌─ 路径 1 ──────────┐    ┌─ 路径 2 ─┐    ┌─ 路径 3/4 ────┐    ┌─ 路径 6a ────────────────────┐
│ TXT → EPUB        │    │ novelia  │    │ syosetu/wenku8 │    │ lightnovel.app               │
│ convert.py        │    │ API 抓取  │    │ CDP + Cloudflare│    │ Dart 桥接 → HTML(+插图)       │
│ (纯本地, 纯stdlib) │    │ web_fetch.py │  │ syosetu_fetch   │    │ lightnovel_api.py             │
└───────────────────┘    └──────────┘    └─────────────────┘    └──────────────┬────────────────┘
                                                                              │
                                                          ┌───────────────────┘
                                                          │  html2epub_font.py
                                                          │  WOFF2→TTF + 插图嵌入
                                                          │  输出: 字体嵌入 EPUB (兼容 Kindle/Kobo)
                                                          │
                                                          │  [实验] font_decode_map.json 存在时
                                                          │  自动解码为纯文本, 跳过字体嵌入
                                                          │  → 输出: 纯 Unicode EPUB (任何阅读器)
```

## 六条路径

### 1. 本地 TXT → EPUB

```bash
python scripts/convert.py "novel.txt" -o "novel.epub" --title "书名" --author "作者"
```

纯 Python stdlib，编码检测 + 章节识别 + 段落合并 + 噪音清洗。

### 2. novelia.cc API 抓取（最快，~30s/100章）

```bash
python scripts/web_fetch.py "https://n.novelia.cc/novel/hameln/68239" -o novel.txt
python scripts/convert.py novel.txt -o novel.epub --title "书名"
```

翻译源: `-t sakura`(推荐) / `jp` / `youdao` / `gpt`

### 3. syosetu.org CDP 抓取（Cloudflare 穿透）

```bash
# TXT 模式
python scripts/syosetu_fetch.py -u "https://syosetu.org/novel/68239/" -o novel.txt
python scripts/convert.py novel.txt -o novel.epub --title "书名"

# HTML 模式（含插图下载）
python scripts/syosetu_fetch.py -u "https://syosetu.org/novel/68239/" \
    --html "D:/path/to/output" --delay 0.5

# 然后打包 EPUB
python scripts/html2epub_font.py "D:/path/to/output" -o "output.epub" --author "作者名"
```

`--visible` 显示浏览器 / `--start 1 --end 5` 只抓前5章 / `--delay 0.3` 加速

### 4. wenku8.net CDP 抓取

```bash
python scripts/wenku8_fetch.py -u "https://www.wenku8.net/novel/3/3988/index.htm" -o novel.txt
python scripts/convert.py novel.txt -o novel.epub --title "书名"
```

### 5. lightnovel.app 字体解码（离线/手动快照）

```bash
python scripts/lightnovel_decode.py snapshot.txt -o output.txt
python scripts/lightnovel_decode.py --html "<div>...</div>" --font-url "https://api.lightnovel.life/font/xxx.woff2"
```

### 6a. lightnovel.app API 抓取（Dart 桥接，推荐）

```bash
# 单章 → 自动存入 D:\YyumekO\Documents\ebook\fetch\{书名}\
python scripts/lightnovel_api.py --bid 17028 --chapter 8 --html

# 指定路径
python scripts/lightnovel_api.py --bid 17028 --chapter 8 --html my.html

# 批量抓取全部章节
for ch in $(seq 1 18); do
  python scripts/lightnovel_api.py --bid 17028 --chapter $ch --html
done

# HTML → 字体嵌入 EPUB（WOFF2 自动转 TTF，兼容更广）
python scripts/html2epub_font.py "D:\YyumekO\Documents\ebook\fetch\Sword Art Online刀剑神域 Progressive 009" \
    -o "SAO_Progressive_009.epub" --author "川原礫"

# HTML → 单章纯文本（VLM OCR，需要 vision-reader skill）
python scripts/html2txt.py "chapter.html"
```

**特性:**
- 自动下载章节插图 → `images/` 目录
- 插图 `src` 改写为本地路径
- 自动生成 `book_info.json`（书名、章节目录、封面引用）
- EPUB 打包时 WOFF2 → TTF 自动转换（`fontTools`）
- 如果 `font_decode_map.json` 存在，自动解码为纯 Unicode（跳过字体嵌入）

**EPUB 输出:** TTF 字体嵌入 + 插图嵌入 → 兼容 Apple Books / Kindle / Kobo / Google Play Books / Thorium / Calibre

**Kindle 特别说明:** 侧载后需在 Aa 菜单开启 "Publisher Font"。或用 KFX 格式（Calibre + KFX Output 插件）稳定性更好。

### 6b. [实验] OCR 纯文本解码

VLM OCR 字形对照表方案，输出完全无需字体的纯 Unicode EPUB。但由于 VLM 对相似字形（未/末、己/已等）有偶发误判，当前作为实验草案保留。

```bash
# 流程:
# 1. 生成字形对照表 → 截屏 → VLM 批量识别 → 建立 font_decode_map.json
# 2. 之后抓取自动检测 map 并解码
# 参考: scripts/build_decode_map.py（脚手架脚本）
```

相关文件以 `_` 前缀保存在 book 目录下（`_pua_mapping_raw.json`, `_all_ocr.txt`, `ocr_chunks/`），不会影响正常使用。

## 离线可用性

| 脚本 | 离线可用 | 备注 |
|------|:--------:|------|
| `convert.py` | ✅ | 纯 stdlib |
| `web_fetch.py` | ❌ | 需网络抓取 |
| `syosetu_fetch.py` | ❌ | 需浏览器 + 网络 |
| `wenku8_fetch.py` | ❌ | 需浏览器 + 网络 |
| `lightnovel_decode.py` | ✅ * | 需已下载的字体文件 |
| `lightnovel_api.py` | ❌ | 需网络 + Dart SDK |
| `html2txt.py` | ❌ | 需 Edge + vision-reader API |
| `html2epub_font.py` | ✅ | 已有 HTML + images 时纯本地 |

## 脚本清单

| 脚本 | 用途 | 路径 |
|------|------|------|
| `convert.py` | TXT → EPUB（编码检测+章节识别+排版） | 1 |
| `web_fetch.py` | novelia.cc API 抓取 | 2 |
| `syosetu_fetch.py` | syosetu.org CDP 抓取（TXT / HTML+插图） | 3 |
| `wenku8_fetch.py` | wenku8.net CDP 抓取 | 4 |
| `lightnovel_decode.py` | lightnovel.app 字体解码（离线快照） | 5 |
| `lightnovel_api.py` | lightnovel.app Dart 桥接 API 抓取 | 6a |
| `html2epub_font.py` | HTML 目录 → 字体嵌入 EPUB（WOFF2→TTF + 插图） | 6a/6b |
| `html2txt.py` | HTML → 截屏分条 → VLM OCR 纯文本 | 6a |
| `build_decode_map.py` | [实验] VLM OCR 字形映射表脚手架 | 6b |
| `dart_bridge/` | Dart SignalR WebSocket 桥接（被 lightnovel_api.py 调用） | 6a |

## 支持的章节格式

`第X章` `第X卷 第Y章` `Chapter N` `序章` `楔子` `终章` `尾声` `番外` `后记` `特典` `外传` `断章`
