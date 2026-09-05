#!/usr/bin/env python3
"""把电子书导出的划线/批注变成强力研读的原料。

支持:
  - Kindle `My Clippings.txt`（英文版与中文版）
  - 通用「一段一条」的纯文本导出

用法:
  python3 extract_highlights.py "My Clippings.txt"
  python3 extract_highlights.py "My Clippings.txt" --format json
  python3 extract_highlights.py "My Clippings.txt" --book 卧底经济学 --out notes.md

划线只是原料，不是笔记。导出后仍要走 SKILL.md 的四要素流程。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

SEPARATOR = "=========="

KIND_PATTERNS = [
    (re.compile(r"标注|Highlight|surlignement|Markierung"), "highlight"),
    (re.compile(r"笔记|Note|note"), "note"),
    (re.compile(r"书签|Bookmark"), "bookmark"),
]

PAGE_RE = re.compile(r"(?:第\s*([0-9ivxlcIVXLC\-]+)\s*页|page\s+([0-9ivxlcIVXLC\-]+))", re.I)
LOC_RE = re.compile(r"(?:位置\s*#?\s*([0-9\-]+)|Location\s+([0-9\-]+))", re.I)


@dataclass
class Clipping:
    book: str
    author: str
    kind: str
    page: str
    location: str
    loc_start: int
    text: str

    def to_dict(self) -> dict:
        return {
            "book": self.book,
            "author": self.author,
            "kind": self.kind,
            "page": self.page,
            "location": self.location,
            "text": self.text,
        }


@dataclass
class Book:
    title: str
    author: str
    clippings: list = field(default_factory=list)


def parse_title_line(line: str) -> tuple[str, str]:
    """`书名 (作者)` -> (书名, 作者)。作者里允许有嵌套括号。"""
    line = line.lstrip("﻿").strip()
    if line.endswith(")") or line.endswith("）"):
        depth = 0
        for i in range(len(line) - 1, -1, -1):
            ch = line[i]
            if ch in ")）":
                depth += 1
            elif ch in "(（":
                depth -= 1
                if depth == 0:
                    return line[:i].strip(), line[i + 1 : -1].strip()
    return line, ""


def parse_meta(line: str) -> tuple[str, str, str, int]:
    kind = "highlight"
    for pattern, name in KIND_PATTERNS:
        if pattern.search(line):
            kind = name
            break

    page = ""
    m = PAGE_RE.search(line)
    if m:
        page = (m.group(1) or m.group(2) or "").strip()

    location = ""
    m = LOC_RE.search(line)
    if m:
        location = (m.group(1) or m.group(2) or "").strip()

    loc_start = 0
    head = re.split(r"[-–]", location)[0] if location else ""
    if head.isdigit():
        loc_start = int(head)
    elif page and re.split(r"[-–]", page)[0].isdigit():
        loc_start = int(re.split(r"[-–]", page)[0])

    return kind, page, location, loc_start


def parse_clippings(raw: str) -> list:
    records = raw.replace("\r\n", "\n").replace("\r", "\n").split(SEPARATOR)
    out = []
    for record in records:
        lines = [ln.strip() for ln in record.strip("\n").split("\n")]
        lines = [ln for ln in lines if ln.strip()]
        if len(lines) < 2:
            continue
        title, author = parse_title_line(lines[0])
        meta_line = lines[1]
        if not meta_line.lstrip().startswith("-"):
            # 通用导出：没有元数据行，整段都是正文
            body = "\n".join(lines[1:])
            out.append(Clipping(title, author, "highlight", "", "", 0, body))
            continue
        kind, page, location, loc_start = parse_meta(meta_line)
        body = "\n".join(lines[2:]).strip()
        if not body:
            continue  # 书签没有正文
        out.append(Clipping(title, author, kind, page, location, loc_start, body))
    return out


def dedup(clippings: list) -> list:
    """同一段反复划线只保留最完整的一条。"""
    kept = []
    for c in sorted(clippings, key=lambda x: -len(x.text)):
        norm = re.sub(r"\s+", "", c.text)
        if not norm:
            continue
        redundant = False
        for k in kept:
            if k.kind != c.kind:
                continue
            if norm and norm in re.sub(r"\s+", "", k.text):
                redundant = True
                break
        if not redundant:
            kept.append(c)
    return sorted(kept, key=lambda x: (x.loc_start, x.text[:20]))


def group_by_book(clippings: list) -> "OrderedDict[str, Book]":
    books: OrderedDict[str, Book] = OrderedDict()
    for c in clippings:
        book = books.setdefault(c.book, Book(c.book, c.author))
        if not book.author and c.author:
            book.author = c.author
        book.clippings.append(c)
    for book in books.values():
        book.clippings = dedup(book.clippings)
    return books


def render_markdown(books) -> str:
    lines = []
    for book in books.values():
        header = f"# {book.title}"
        if book.author:
            header += f" — {book.author}"
        lines.append(header)
        highlights = [c for c in book.clippings if c.kind == "highlight"]
        notes = [c for c in book.clippings if c.kind == "note"]
        lines.append("")
        lines.append(f"> 划线 {len(highlights)} 条，批注 {len(notes)} 条。")
        lines.append("> 这是原料，不是笔记。下一步：按四要素（脉络/亮点/心得/联系）加工。")
        lines.append("")
        for c in book.clippings:
            if c.kind == "bookmark":
                continue
            where = c.page and f"p. {c.page}" or (c.location and f"loc. {c.location}") or ""
            if c.kind == "note":
                lines.append(f"【{c.text}】" + (f" <!-- {where} -->" if where else ""))
            else:
                lines.append(f"> {c.text}" + (f"\n>\n> — {where}" if where else ""))
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kindle / 电子书划线导出 → 强力研读原料")
    ap.add_argument("path", help="My Clippings.txt 或其他导出文本")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--book", help="只导出书名包含该关键词的书")
    ap.add_argument("--out", help="输出文件，默认打印到 stdout")
    args = ap.parse_args(argv)

    src = Path(args.path)
    if not src.is_file():
        print(f"找不到文件: {src}", file=sys.stderr)
        return 1

    raw = src.read_text(encoding="utf-8-sig", errors="replace")
    books = group_by_book(parse_clippings(raw))

    if args.book:
        needle = args.book.lower()
        books = OrderedDict(
            (k, v) for k, v in books.items() if needle in k.lower()
        )
    if not books:
        print("没有解析到任何划线。", file=sys.stderr)
        return 1

    if args.format == "json":
        payload = [
            {
                "book": b.title,
                "author": b.author,
                "clippings": [c.to_dict() for c in b.clippings],
            }
            for b in books.values()
        ]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = render_markdown(books)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        total = sum(len(b.clippings) for b in books.values())
        print(f"已写入 {args.out}：{len(books)} 本书，{total} 条。", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
