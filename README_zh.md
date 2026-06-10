# txt-to-epub

> 多源轻小说抓取 + 自动排版 + 字体嵌入 EPUB 生成工具

[English](README.md)

支持从 lightnovel.app、syosetu.org、wenku8.net、novelia.cc 抓取小说，输出字体嵌入 EPUB，兼容 Kindle、Kobo、Apple Books 等主流阅读器。

## 功能特性

- **4 个抓取源** — lightnovel.app（Dart SignalR 桥接）、syosetu.org（CDP Cloudflare 穿透）、wenku8.net（CDP）、novelia.cc（REST API）
- **字体嵌入 EPUB** — 自动下载 WOFF2 字体并转换为 TTF 嵌入，解决 lightnovel.app 字体混淆问题
- **插图嵌入** — 自动下载章节插图，改写为本地路径，嵌入 EPUB
- **抓取记忆** — 按章节记录已抓取内容，重复运行时自动跳过，支持连载追更
- **`--all` 自动压制** — 抓取全部章节后自动生成 EPUB，一步到位
- **`--pack-only`** — 已有抓取文件时直接压制，无需重新抓取
- **编码检测 + 章节识别** — 智能识别 TXT 编码和章节标题，段落合并，噪音清洗
- **预编译 Dart bridge** — 无需 Dart SDK，开箱即用（Windows）

## 快速开始

```bash
# 1. 解压 txt-to-epub-dist.zip
# 2. 一键安装依赖
setup.bat

# 3. 编辑 config.json，填入 lightnovel.app refresh_token（可选，仅 lightnovel 源需要）
#    浏览器打开 lightnovel.app → F12 → Application → Local Storage
#    Key: sb-yywiuxedvyfxdpznoyqy-auth-token

# 4. 开始使用
python ebook.py lightnovel --bid <BID> --all                       # 抓取 + 自动压制 EPUB
python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"
python ebook.py syosetu -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>
python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "书名" --author "作者"
```

## 支持源

| 源 | 方式 | 字体 EPUB | 插图 | 需要 |
|---|------|:---------:|:----:|------|
| [lightnovel.app](https://www.lightnovel.app) | Dart SignalR 桥接 | ✅ | ✅ | refresh_token |
| [syosetu.org](https://syosetu.org) | CDP（Edge 浏览器） | — | ✅ | Edge 浏览器 |
| [wenku8.net](https://www.wenku8.net) | CDP（Edge 浏览器） | — | ✅ | Edge 浏览器 |
| [novelia.cc](https://novelia.cc) | REST API | — | — | — |
| 本地 TXT | stdlib | — | — | — |

## 安装

### 前置条件

- Python 3.9+
- Microsoft Edge（syosetu/wenku8 CDP 抓取需要；自动探测路径）

### 安装步骤

```bash
# 自动安装（Windows）
setup.bat

# 手动安装
pip install -r requirements.txt
```

Dart bridge 已**预编译**为 `lightnovel_bridge.exe`，无需安装 Dart SDK。  
如需从源码重新编译：`winget install Google.DartSDK` 然后 `dart compile exe bin/lightnovel_bridge.dart`。

## 配置

复制 `config.example.json` 为 `config.json`，填入你的设置：

```json
{
  "lightnovel": {
    "refresh_token": "你的 REFRESH_TOKEN"
  },
  "fetch_dir": "./fetch",
  "edge_path": null
}
```

也可用环境变量覆盖：`LIGHTNOVEL_REFRESH_TOKEN`、`FETCH_DIR`、`EDGE_PATH`。

## 用法

### 统一入口（`ebook.py`）

```bash
# lightnovel.app
python ebook.py lightnovel --bid <BID> --chapter <CID> --html     # 单章 HTML
python ebook.py lightnovel --bid <BID> --all                       # 全部章节 → 自动 EPUB
python ebook.py lightnovel --bid <BID> --all --no-epub             # 只抓取不压制
python ebook.py lightnovel --bid <BID> --pack-only                 # 已有文件直接压制
python ebook.py lightnovel --bid <BID> --pack-only --author "作者名"

# syosetu.org
python ebook.py syosetu -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>

# wenku8.net
python ebook.py wenku8 "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"

# novelia.cc
python ebook.py novelia "https://n.novelia.cc/novel/<SOURCE>/<ID>" -o <OUTPUT>.txt

# TXT → EPUB
python ebook.py convert <INPUT>.txt -o <OUTPUT>.epub --title "书名" --author "作者"

# HTML 目录 → 字体嵌入 EPUB
python ebook.py pack <BOOK_DIR> --author "作者名"

# 管理抓取记忆
python ebook.py memory list              # 列出所有记录
python ebook.py memory show lightnovel   # 查看详情
python ebook.py memory forget <BID>      # 清除某本书的记忆
```

### 通用选项

| 选项 | 说明 |
|------|------|
| `--all` | 抓取全部章节，默认生成 EPUB |
| `--force` | 忽略记忆，强制重新抓取 |
| `--pack-only` | 跳过抓取，直接用已有文件压制 EPUB |
| `--no-epub` | 抓取但不自动压制 EPUB |
| `--author NAME` | 指定作者（嵌入 EPUB 元数据） |
| `-o, --output PATH` | 指定 EPUB 输出路径 |

### 直接调用脚本

`scripts/` 下的每个脚本都可以独立运行：

```bash
python scripts/convert.py <INPUT>.txt -o <OUTPUT>.epub --title "书名"
python scripts/lightnovel_api.py --bid <BID> --chapter <CID> --html
python scripts/syosetu_fetch.py -u "https://syosetu.org/novel/<ID>/" --html <OUT_DIR>
python scripts/wenku8_fetch.py -u "https://www.wenku8.net/novel/<CAT>/<ID>/index.htm"
python scripts/lightnovel_decode.py --html-file page.html --font-url "https://..."
```

## 工作原理

```
┌─ lightnovel.app ───────────────────────────────────────────────┐
│  Dart SignalR bridge (.exe)                                    │
│    → HTTP token 交换 → WebSocket MessagePack → gzip 解压       │
│    → WOFF2 字体下载（curl_cffi TLS 伪装）                      │
│    → PUA 码位剔除（fontTools）                                  │
│    → 生成 base64 字体嵌入的自包含 HTML                           │
│    → 图片提取与下载                                             │
└──────────────────────┬─────────────────────────────────────────┘
                       │  html2epub_font.py
                       │  WOFF2 → TTF（fontTools）
                       │  XHTML + CSS + EPUB3 打包
                       ▼
                 ┌──────────┐
                 │  .epub   │  ◄── 字体嵌入 + 插图嵌入
                 └──────────┘      Kindle / Kobo / Apple Books
```

### 为什么需要 Dart Bridge？

lightnovel.app 的 SignalR WebSocket 服务器会进行 **TLS 指纹检测**——标准的 Python WebSocket 库（`websockets`、`websocket-client`）会被拒绝。Dart 原生的 `dart:io` HttpClient 使用 BoringSSL，指纹与 Novella 手机 App 一致，因此可以通过验证。

### 为什么需要字体嵌入？

lightnovel.app 将章节文字映射到 **Unicode PUA 私用区**（U+E000–F8FF），正确的字形存储在 WOFF2 字体文件中。通过将字体转换为 TTF 并嵌入 EPUB，任何阅读器都能正确显示文字，无需安装原始字体。

## Kindle 兼容性

- 侧载 EPUB 后在 Aa 菜单中开启「Publisher Font / 出版商字体」
- 最佳方案：通过 Calibre + KFX Output 插件转换为 KFX 格式

## 脚本清单

| 脚本 | 用途 |
|------|------|
| `ebook.py` | 统一 CLI 入口 |
| `scripts/convert.py` | TXT → EPUB（编码检测、章节识别、噪音清洗） |
| `scripts/lightnovel_api.py` | lightnovel.app Dart 桥接 API 抓取 |
| `scripts/syosetu_fetch.py` | syosetu.org CDP 抓取（Cloudflare 穿透） |
| `scripts/wenku8_fetch.py` | wenku8.net CDP 抓取 |
| `scripts/web_fetch.py` | novelia.cc REST API 抓取 |
| `scripts/html2epub_font.py` | HTML 目录 → 字体嵌入 EPUB（WOFF2→TTF） |
| `scripts/lightnovel_decode.py` | 离线字体解码（快照用） |
| `scripts/fetch_memory.py` | 抓取记忆持久化 |
| `scripts/html2txt.py` | [实验] VLM OCR 管线 |
| `scripts/build_decode_map.py` | [实验] VLM OCR 字形映射表 |
| `scripts/dart_bridge/` | Dart SignalR WebSocket 桥接 |

## 依赖

```
requests  websocket-client  fonttools  brotli  curl_cffi  Pillow
```

全部可通过 `pip install -r requirements.txt` 安装。  
Dart bridge 已预编译，无需 Dart SDK。

## 注意事项

- **配置安全**：`config.json` 包含你的 lightnovel.app refresh_token，已被发行包和 `.gitignore` 排除。
- **记忆数据**：抓取记忆存储在 `fetch_memory.json` 中，属个人数据，不包含在发行包内。
- **实验性 OCR**：VLM OCR 字体解码方案（`build_decode_map.py`、`html2txt.py`）作为草案保留，对相似字形（未/末、己/已等）存在偶发误判。
- **跨平台**：主要在 Windows 上开发。Dart bridge `.exe` 仅限 Windows；非 Windows 用户需安装 Dart SDK 运行 `dart run`。

## 许可证

MIT
