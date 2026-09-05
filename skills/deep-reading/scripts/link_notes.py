#!/usr/bin/env python3
"""维护读书笔记库的连接网络。

「读的书多到一定程度就会发现书与书之间存在联系。」这个脚本把 [[链接]] 变成
可查询的网：生成索引、写入反向链接、报告孤岛与待写清单。

用法:
  python3 link_notes.py ~/notes/reading --report
  python3 link_notes.py ~/notes/reading --index
  python3 link_notes.py ~/notes/reading --backlinks
  python3 link_notes.py ~/notes/reading --index --backlinks --report

只依赖标准库。--index / --backlinks 会写文件，先用 --report 看看再动手。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

LINK_RE = re.compile(r"\[\[([^\]\|#]+?)(?:\|[^\]]*)?\]\]")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
BL_START = "<!-- backlinks:start -->"
BL_END = "<!-- backlinks:end -->"
IDX_START = "<!-- index:start -->"
IDX_END = "<!-- index:end -->"

SKIP_NAMES = {"index", "readme", "note-template", "library-index-template"}


class Note:
    def __init__(self, path: Path, root: Path):
        self.path = path
        self.rel = path.relative_to(root)
        self.key = path.stem
        self.raw = path.read_text(encoding="utf-8", errors="replace")
        self.meta = parse_frontmatter(self.raw)
        body = strip_frontmatter(self.raw)
        # 反向链接区里的链接不算出链，否则会自我循环
        body_wo_bl = re.sub(
            re.escape(BL_START) + r".*?" + re.escape(BL_END), "", body, flags=re.S
        )
        self.links = unique(LINK_RE.findall(body_wo_bl))
        m = H1_RE.search(body)
        self.title = (m.group(1).strip() if m else self.key)
        self.field = self.meta.get("field", "未分类")

    @property
    def aliases(self):
        names = {self.key, self.title}
        for k in ("book", "title", "alias"):
            if self.meta.get(k):
                names.add(self.meta[k])
        return {n.strip() for n in names if n and n.strip()}


def unique(items):
    seen, out = set(), []
    for i in items:
        i = i.strip()
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def parse_frontmatter(raw: str) -> dict:
    if not raw.startswith("---"):
        return {}
    end = raw.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in raw[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip("\"'")
    return meta


def strip_frontmatter(raw: str) -> str:
    if not raw.startswith("---"):
        return raw
    end = raw.find("\n---", 3)
    return raw[end + 4 :] if end != -1 else raw


def load_notes(root: Path) -> list:
    notes = []
    for path in sorted(root.rglob("*.md")):
        if path.stem.lower() in SKIP_NAMES or any(
            part.startswith(".") for part in path.parts
        ):
            continue
        notes.append(Note(path, root))
    return notes


def build_resolver(notes: list) -> dict:
    resolver = {}
    for n in notes:
        for alias in n.aliases:
            resolver.setdefault(alias, n)
    return resolver


def analyse(notes: list):
    resolver = build_resolver(notes)
    backlinks = defaultdict(list)
    dangling = defaultdict(list)
    for n in notes:
        for target in n.links:
            hit = resolver.get(target)
            if hit is None:
                dangling[target].append(n)
            elif hit is not n:
                if n.key not in [b.key for b in backlinks[hit.key]]:
                    backlinks[hit.key].append(n)
    return resolver, backlinks, dangling


def cmd_report(notes, backlinks, dangling) -> None:
    print(f"笔记 {len(notes)} 篇。\n")

    islands = [n for n in notes if not n.links and not backlinks.get(n.key)]
    print(f"## 孤岛笔记（{len(islands)}）—— 缺第 ④ 要素，知识没进网络")
    for n in islands:
        print(f"  - {n.rel}")
    if not islands:
        print("  （无）")

    print(f"\n## 待写笔记（{len(dangling)}）—— 被引用但还不存在")
    for target, sources in sorted(dangling.items(), key=lambda x: -len(x[1])):
        who = "、".join(s.title for s in sources[:3])
        more = f" 等 {len(sources)} 处" if len(sources) > 3 else ""
        print(f"  - [[{target}]]  ← {who}{more}")
    if not dangling:
        print("  （无）")

    ranked = sorted(notes, key=lambda n: -len(backlinks.get(n.key, [])))
    ranked = [n for n in ranked if backlinks.get(n.key)]
    print(f"\n## 枢纽笔记 —— 被引用最多，通常是该领域的地基")
    for n in ranked[:10]:
        print(f"  - {n.title} ← {len(backlinks[n.key])} 篇")
    if not ranked:
        print("  （无）")

    hot = [t for t, s in dangling.items() if len(s) >= 3]
    hot += [n.title for n in ranked if len(backlinks[n.key]) >= 3]
    if hot:
        print("\n## 该写文章了")
        print("  同一个议题已经在 3 篇以上的笔记里出现——你已经能评判作者之间的高下了：")
        for h in unique(hot):
            print(f"  - {h}")


def replace_block(text: str, start: str, end: str, payload: str) -> str:
    block = f"{start}\n{payload}\n{end}"
    if start in text and end in text:
        return re.sub(
            re.escape(start) + r".*?" + re.escape(end), lambda _: block, text, flags=re.S
        )
    return text.rstrip() + "\n\n" + block + "\n"


def cmd_backlinks(notes, backlinks) -> int:
    changed = 0
    for n in notes:
        srcs = backlinks.get(n.key, [])
        lines = ["## 反向链接", ""]
        lines += [f"- [[{s.key}]] — {s.title}" for s in srcs] or ["- （暂无）"]
        new = replace_block(n.raw, BL_START, BL_END, "\n".join(lines))
        if new != n.raw:
            n.path.write_text(new, encoding="utf-8")
            changed += 1
    print(f"反向链接已更新：{changed} 篇改动 / 共 {len(notes)} 篇。", file=sys.stderr)
    return changed


def cmd_index(root: Path, notes, backlinks) -> Path:
    by_field = defaultdict(list)
    for n in notes:
        by_field[n.field].append(n)

    lines = []
    for fieldname in sorted(by_field):
        group = sorted(by_field[fieldname], key=lambda n: n.meta.get("read", "") or n.title)
        lines.append(f"### {fieldname}")
        lines.append("")
        for n in group:
            bits = [f"- [[{n.key}]]"]
            if n.meta.get("read"):
                bits.append(f"· {n.meta['read']}")
            nb = len(backlinks.get(n.key, []))
            if nb:
                bits.append(f"· ←{nb}")
            lines.append(" ".join(bits))
        lines.append("")

    index_path = root / "INDEX.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else "# 读书笔记库\n"
    index_path.write_text(
        replace_block(existing, IDX_START, IDX_END, "\n".join(lines).rstrip()),
        encoding="utf-8",
    )
    print(f"索引已写入 {index_path}", file=sys.stderr)
    return index_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="维护读书笔记库的 [[链接]] 网络")
    ap.add_argument("root", help="笔记目录")
    ap.add_argument("--report", action="store_true", help="只读：孤岛 / 待写 / 枢纽")
    ap.add_argument("--index", action="store_true", help="写入 INDEX.md")
    ap.add_argument("--backlinks", action="store_true", help="在每篇笔记写入反向链接区")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"不是目录: {root}", file=sys.stderr)
        return 1

    notes = load_notes(root)
    if not notes:
        print(f"{root} 下没有找到 .md 笔记。", file=sys.stderr)
        return 1

    _, backlinks, dangling = analyse(notes)

    if not (args.report or args.index or args.backlinks):
        args.report = True

    if args.backlinks:
        cmd_backlinks(notes, backlinks)
        notes = load_notes(root)
        _, backlinks, dangling = analyse(notes)
    if args.index:
        cmd_index(root, notes, backlinks)
    if args.report:
        cmd_report(notes, backlinks, dangling)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
