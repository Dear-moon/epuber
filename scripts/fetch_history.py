#!/usr/bin/env python3
"""
时间轴抓取记录。所有抓取来源成功后各记一条，写到 {ebook_root}/fetch_records.json，
按 time 升序（旧→新）排列。

记录字段:
  time       抓取完成时间
  source     来源: novelia / wenku / lk / lightnovel / syosetu / wenku8
  kind       类型: web / wenku / app
  title      书名
  id         书 ID（novelia 为 source/id，wenku 为 wid，lk 为书号…）
  count      章节数(web/app) 或 卷数(wenku)
  count_type chapters / volumes
  file       产出文件或目录
"""

import json
import sys
import time
from pathlib import Path

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    from config import get_fetch_dir
except ImportError:
    def get_fetch_dir():
        return Path.cwd() / "fetch"


def records_path():
    """fetch_records.json 位于 ebook 根目录（fetch_dir 的上级）。"""
    fetch_dir = get_fetch_dir()
    root = fetch_dir.parent if fetch_dir.name == 'fetch' else fetch_dir
    return root / 'fetch_records.json'


def record(source, title, book_id, count, count_type, file=None, kind=''):
    """Append one fetch event; keep the list sorted ascending by time."""
    rec = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': source,
        'kind': kind,
        'title': title or '',
        'id': str(book_id),
        'count': int(count) if count is not None else 0,
        'count_type': count_type or 'chapters',
        'file': str(file or ''),
    }
    path = records_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    data.append(rec)
    data.sort(key=lambda r: r.get('time', ''))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"  [record] {rec['source']} · {rec['title'][:30] or rec['id']} "
          f"({rec['count']} {rec['count_type']}) → {path.name}")
    return rec


if __name__ == '__main__':
    # Manual test: python fetch_history.py list
    import sys
    p = records_path()
    if len(sys.argv) > 1 and sys.argv[1] == 'list':
        if p.exists():
            for r in json.loads(p.read_text(encoding='utf-8')):
                print(f"  {r['time']}  {r['source']}/{r['kind']}  "
                      f"{r['title'][:40] or r['id']}  {r['count']}{r['count_type']}  {r['file']}")
        else:
            print(f"No records yet: {p}")
    else:
        print(f"records file: {p}")
