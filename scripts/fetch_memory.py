#!/usr/bin/env python3
"""
Fetch memory — tracks which chapters of which novels have been fetched.
Stored as JSON at ~/.claude/skills/txt-to-epub/fetch_memory.json

Keys: f"{source}:{novel_id}" — e.g. "lightnovel:17028", "syosetu:68239"
"""

import json, time
from pathlib import Path

MEMORY_FILE = Path(__file__).resolve().parent.parent / "fetch_memory.json"


class FetchMemory:
    def __init__(self):
        self._data = None

    def _load(self):
        if self._data is None:
            if MEMORY_FILE.exists():
                self._data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            else:
                self._data = {}
        return self._data

    def _save(self):
        MEMORY_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _key(self, source, novel_id):
        return f"{source}:{novel_id}"

    # ---- query ----

    def is_fetched(self, source, novel_id, chapter_num):
        """Check if a specific chapter has been fetched."""
        entry = self._load().get(self._key(source, novel_id), {})
        return str(chapter_num) in entry.get("chapters", {})

    def get_fetched_chapters(self, source, novel_id):
        """Return set of fetched chapter numbers."""
        entry = self._load().get(self._key(source, novel_id), {})
        return {int(k) for k in entry.get("chapters", {})}

    def get_novel(self, source, novel_id):
        """Return full entry dict for a novel."""
        return self._load().get(self._key(source, novel_id))

    def list_all(self):
        """Return list of (key, entry) for all tracked novels."""
        data = self._load()
        result = []
        for k, v in data.items():
            src, nid = k.split(":", 1)
            result.append((src, nid, v))
        return result

    # ---- mark ----

    def mark_fetched(self, source, novel_id, chapter_num, title="", extra=None):
        """Record a chapter as fetched."""
        data = self._load()
        key = self._key(source, novel_id)
        if key not in data:
            data[key] = {
                "title": title or "",
                "chapters": {},
                "first_fetch": time.strftime("%Y-%m-%d %H:%M"),
                "extra": extra or {},
            }
        entry = data[key]
        entry["chapters"][str(chapter_num)] = time.strftime("%Y-%m-%d %H:%M")
        entry["last_fetch"] = time.strftime("%Y-%m-%d %H:%M")
        if title:
            entry["title"] = title
        if extra:
            entry["extra"] = extra
        self._save()

    # ---- forget ----

    def forget_novel(self, source, novel_id):
        """Remove an entire novel from memory."""
        data = self._load()
        key = self._key(source, novel_id)
        if key in data:
            del data[key]
            self._save()
            return True
        return False

    def forget_chapter(self, source, novel_id, chapter_num):
        """Remove a single chapter from memory."""
        data = self._load()
        key = self._key(source, novel_id)
        if key in data:
            data[key]["chapters"].pop(str(chapter_num), None)
            if not data[key]["chapters"]:
                del data[key]
            self._save()
            return True
        return False


# ---- CLI ----

def cmd_memory(args):
    """Manage fetch memory."""
    mem = FetchMemory()

    if not args or args[0] == "list":
        all_entries = mem.list_all()
        if not all_entries:
            print("No fetch memory yet.")
            return
        for src, nid, entry in all_entries:
            title = entry.get("title", nid)
            ch_count = len(entry.get("chapters", {}))
            last = entry.get("last_fetch", "?")
            print(f"  {src}:{nid}  {title}")
            print(f"    {ch_count} chapters, last: {last}")

    elif args[0] == "forget" and len(args) >= 3:
        src, nid = args[1], args[2]
        if len(args) >= 4:
            mem.forget_chapter(src, nid, int(args[3]))
            print(f"Forgot chapter {args[3]} of {src}:{nid}")
        else:
            mem.forget_novel(src, nid)
            print(f"Forgot entire novel {src}:{nid}")

    elif args[0] == "show" and len(args) >= 3:
        src, nid = args[1], args[2]
        entry = mem.get_novel(src, nid)
        if entry:
            chs = sorted(entry.get("chapters", {}).keys(), key=int)
            print(f"{entry.get('title', nid)} ({src}:{nid})")
            print(f"Fetched: {len(chs)} chapters")
            if chs:
                print(f"Range: {chs[0]} - {chs[-1]}")
                # Show gaps
                nums = sorted(int(c) for c in chs)
                gaps = []
                for i in range(1, len(nums)):
                    if nums[i] != nums[i-1] + 1:
                        gaps.append(f"{nums[i-1]+1}-{nums[i]-1}")
                if gaps:
                    print(f"Gaps: {', '.join(gaps[:10])}{'...' if len(gaps)>10 else ''}")
            print(f"Last fetch: {entry.get('last_fetch', '?')}")
        else:
            print(f"No memory for {src}:{nid}")

    elif args[0] == "clear":
        MEMORY_FILE.unlink(missing_ok=True)
        mem._data = {}
        print("Memory cleared.")

    else:
        print("Usage:")
        print("  python fetch_memory.py list                    # list all")
        print("  python fetch_memory.py show <source> <id>     # show one novel")
        print("  python fetch_memory.py forget <source> <id> [chapter]  # forget")
        print("  python fetch_memory.py clear                   # clear all")


if __name__ == "__main__":
    import sys
    cmd_memory(sys.argv[1:])
