#!/usr/bin/env python3
"""把电子书的划线/批注变成强力研读的原料。

支持三个来源：
  Apple Books   读本机标注数据库，无需导出
  Kindle        `My Clippings.txt`（中英文版皆可）
  通用文本      一段一条的导出文件

用法:
  python3 extract_highlights.py --list                      # 有划线的书都列出来
  python3 extract_highlights.py --source apple              # Apple Books 全部
  python3 extract_highlights.py --source apple --book 卧底   # 只要某本
  python3 extract_highlights.py "My Clippings.txt"          # Kindle，自动识别
  python3 extract_highlights.py --source apple --format json -o raw.json

不给 path 也不给 --source 时，自动去找 Apple Books。

划线只是原料，不是笔记。导出后仍要走 SKILL.md 的四要素流程。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- 通用结构

SEPARATOR = "=========="

KIND_PATTERNS = [
    (re.compile(r"标注|Highlight|surlignement|Markierung"), "highlight"),
    (re.compile(r"笔记|Note|note"), "note"),
    (re.compile(r"书签|Bookmark"), "bookmark"),
]

PAGE_RE = re.compile(r"(?:第\s*([0-9ivxlcIVXLC\-]+)\s*页|page\s+([0-9ivxlcIVXLC\-]+))", re.I)
LOC_RE = re.compile(r"(?:位置\s*#?\s*([0-9\-]+)|Location\s+([0-9\-]+))", re.I)

APPLE_STYLES = {0: "下划线", 1: "绿", 2: "蓝", 3: "黄", 4: "粉", 5: "紫"}
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


@dataclass
class Clipping:
    book: str
    author: str = ""
    kind: str = "highlight"        # highlight | note | bookmark
    text: str = ""                 # 划线原文
    note: str = ""                 # 附在这条划线上的批注
    chapter: str = ""
    page: str = ""
    location: str = ""
    style: str = ""
    added: str = ""
    loc_start: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "book": self.book, "author": self.author, "kind": self.kind,
            "text": self.text, "note": self.note, "chapter": self.chapter,
            "page": self.page, "location": self.location,
            "style": self.style, "added": self.added,
        }
        return {k: v for k, v in d.items() if v}


@dataclass
class Book:
    title: str
    author: str = ""
    clippings: list = field(default_factory=list)


# ---------------------------------------------------------------- Apple Books

APPLE_ROOT = Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents"
ANNOTATION_DIR = APPLE_ROOT / "AEAnnotation"
LIBRARY_DIR = APPLE_ROOT / "BKLibrary"

APPLE_HELP = """找不到 Apple Books 的标注数据库。可能的原因：

  1. 这台机器上还没用 Apple Books 划过线（数据库要划过第一条才会建）。
  2. 终端没有「完全磁盘访问权限」——这是最常见的原因。
     系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 把 终端 / iTerm /
     Claude 所在的 App 打开，然后重开终端再试。
  3. 你的标注只在 iPhone/iPad 上，还没同步到这台 Mac。
     打开 Mac 上的「图书」App 等它同步完再跑。

期望路径：
  %s
""" % ANNOTATION_DIR


def _open_readonly(db: Path, workdir: Path) -> sqlite3.Connection:
    """Books 的库常带 WAL 且被占用，先复制再只读打开。"""
    local = workdir / db.name
    shutil.copy2(db, local)
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            shutil.copy2(side, workdir / side.name)
    return sqlite3.connect(f"file:{local}?mode=ro", uri=True)


def _columns(con: sqlite3.Connection, table: str) -> set:
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _pick(available: set, *names: str):
    """挑第一个真实存在的列名。macOS 各版本的 schema 有出入。"""
    for n in names:
        if n in available:
            return n
    return None


def _core_data_time(value) -> str:
    if not value:
        return ""
    try:
        return (CORE_DATA_EPOCH + timedelta(seconds=float(value))).astimezone().strftime(
            "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError, OverflowError):
        return ""


def apple_library(workdir: Path) -> dict:
    """asset id -> (书名, 作者)"""
    out = {}
    for db in sorted(LIBRARY_DIR.glob("*.sqlite")) if LIBRARY_DIR.is_dir() else []:
        try:
            con = _open_readonly(db, workdir)
        except (OSError, sqlite3.Error):
            continue
        cols = _columns(con, "ZBKLIBRARYASSET")
        asset = _pick(cols, "ZASSETID")
        title = _pick(cols, "ZTITLE", "ZSORTTITLE")
        author = _pick(cols, "ZAUTHOR", "ZSORTAUTHOR")
        if asset and title:
            fields = ", ".join(x for x in (asset, title, author) if x)
            try:
                for row in con.execute(f"SELECT {fields} FROM ZBKLIBRARYASSET"):
                    out[row[0]] = (row[1] or "", (row[2] if author else "") or "")
            except sqlite3.Error:
                pass
        con.close()
    return out


def apple_clippings() -> list:
    if not ANNOTATION_DIR.is_dir() or not list(ANNOTATION_DIR.glob("*.sqlite")):
        raise FileNotFoundError(APPLE_HELP)

    clippings = []
    with tempfile.TemporaryDirectory(prefix="deep-reading-") as tmp:
        workdir = Path(tmp)
        library = apple_library(workdir)

        for db in sorted(ANNOTATION_DIR.glob("*.sqlite")):
            try:
                con = _open_readonly(db, workdir)
            except (OSError, sqlite3.Error):
                continue
            cols = _columns(con, "ZAEANNOTATION")
            if not cols:
                con.close()
                continue

            spec = {
                "asset": _pick(cols, "ZANNOTATIONASSETID"),
                "text": _pick(cols, "ZANNOTATIONSELECTEDTEXT", "ZANNOTATIONREPRESENTATIVETEXT"),
                "note": _pick(cols, "ZANNOTATIONNOTE"),
                "chapter": _pick(cols, "ZFUTUREPROOFING5", "ZANNOTATIONCHAPTER"),
                "style": _pick(cols, "ZANNOTATIONSTYLE"),
                "created": _pick(cols, "ZANNOTATIONCREATIONDATE"),
                "location": _pick(cols, "ZANNOTATIONLOCATION"),
                "order": _pick(cols, "ZPLLOCATIONRANGESTART", "ZANNOTATIONCREATIONDATE"),
                "deleted": _pick(cols, "ZANNOTATIONDELETED"),
            }
            usable = [k for k, v in spec.items() if v]
            select = ", ".join(spec[k] for k in usable)
            where = f"WHERE {spec['deleted']} = 0" if spec["deleted"] else ""
            try:
                rows = con.execute(f"SELECT {select} FROM ZAEANNOTATION {where}").fetchall()
            except sqlite3.Error:
                con.close()
                continue

            for row in rows:
                rec = dict(zip(usable, row))
                text = (rec.get("text") or "").strip()
                note = (rec.get("note") or "").strip()
                if not text and not note:
                    continue
                title, author = library.get(rec.get("asset"), ("", ""))
                order = rec.get("order")
                clippings.append(
                    Clipping(
                        book=title or rec.get("asset") or "未知书名",
                        author=author,
                        kind="highlight" if text else "note",
                        text=text,
                        note=note,
                        chapter=(rec.get("chapter") or "").strip(),
                        style=APPLE_STYLES.get(rec.get("style"), ""),
                        added=_core_data_time(rec.get("created")),
                        location=str(rec.get("location") or "")[:80],
                        loc_start=float(order) if isinstance(order, (int, float)) else 0.0,
                    )
                )
            con.close()

    if not clippings:
        raise FileNotFoundError(
            "找到了 Apple Books 数据库，但里面没有任何标注。\n"
            "先在「图书」App 里划几条线，或等 iCloud 同步完成。"
        )
    return clippings


# ---------------------------------------------------------------- Kindle / 文本

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


def parse_meta(line: str) -> tuple[str, str, str, float]:
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

    loc_start = 0.0
    head = re.split(r"[-–]", location)[0] if location else ""
    if head.isdigit():
        loc_start = float(head)
    elif page and re.split(r"[-–]", page)[0].isdigit():
        loc_start = float(re.split(r"[-–]", page)[0])

    return kind, page, location, loc_start


def parse_clippings(raw: str) -> list:
    records = raw.replace("\r\n", "\n").replace("\r", "\n").split(SEPARATOR)
    out = []
    for record in records:
        lines = [ln.strip() for ln in record.strip("\n").split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        title, author = parse_title_line(lines[0])
        meta_line = lines[1]
        if not meta_line.lstrip().startswith("-"):
            out.append(Clipping(book=title, author=author, text="\n".join(lines[1:])))
            continue
        kind, page, location, loc_start = parse_meta(meta_line)
        body = "\n".join(lines[2:]).strip()
        if not body:
            continue  # 书签没有正文
        out.append(
            Clipping(
                book=title, author=author, kind=kind,
                text="" if kind == "note" else body,
                note=body if kind == "note" else "",
                page=page, location=location, loc_start=loc_start,
            )
        )
    return out


# ---------------------------------------------------------------- 整理与输出

def dedup(clippings: list) -> list:
    """同一段反复划线只保留最完整的一条。"""
    kept = []
    for c in sorted(clippings, key=lambda x: -len(x.text + x.note)):
        norm = re.sub(r"\s", "", c.text or c.note)
        if not norm:
            continue
        if any(
            k.kind == c.kind and norm in re.sub(r"\s", "", k.text or k.note)
            for k in kept
        ):
            continue
        kept.append(c)
    return sorted(kept, key=lambda x: (x.loc_start, (x.text or x.note)[:20]))


def group_by_book(clippings: list) -> "OrderedDict[str, Book]":
    books: OrderedDict[str, Book] = OrderedDict()
    for c in clippings:
        book = books.setdefault(c.book, Book(c.book, c.author))
        if not book.author and c.author:
            book.author = c.author
        book.clippings.append(c)
    for book in books.values():
        book.clippings = dedup(book.clippings)
    return OrderedDict(sorted(books.items(), key=lambda kv: -len(kv[1].clippings)))


def render_markdown(books) -> str:
    lines = []
    for book in books.values():
        header = f"# {book.title}"
        if book.author:
            header += f" — {book.author}"
        highlights = [c for c in book.clippings if c.kind == "highlight"]
        notes = [c for c in book.clippings if c.note]
        lines += [
            header, "",
            f"> 划线 {len(highlights)} 条，批注 {len(notes)} 条。",
            "> 这是原料，不是笔记。下一步：按四要素（脉络/亮点/心得/联系）加工。",
            "",
        ]
        current_chapter = None
        for c in book.clippings:
            if c.kind == "bookmark":
                continue
            if c.chapter and c.chapter != current_chapter:
                current_chapter = c.chapter
                lines += [f"## {c.chapter}", ""]
            where = ", ".join(
                x for x in (
                    f"p. {c.page}" if c.page else "",
                    f"loc. {c.location}" if c.location and not c.page else "",
                    c.style,
                ) if x
            )
            if c.text:
                lines.append(f"> {c.text}")
                if where:
                    lines += [">", f"> — {where}"]
            if c.note:
                lines.append(f"【{c.note}】")
            lines.append("")
        lines += ["---", ""]
    return "\n".join(lines).rstrip() + "\n"


def load(args) -> list:
    source = args.source
    if source == "auto":
        source = "apple" if not args.path else "text"
    if source == "apple":
        return apple_clippings()

    src = Path(args.path).expanduser()
    if not src.is_file():
        print(f"找不到文件: {src}", file=sys.stderr)
        raise SystemExit(1)
    return parse_clippings(src.read_text(encoding="utf-8-sig", errors="replace"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Apple Books / Kindle / 文本导出 → 强力研读原料"
    )
    ap.add_argument("path", nargs="?", help="My Clippings.txt 或其他导出文本")
    ap.add_argument(
        "--source", choices=["auto", "apple", "kindle", "text"], default="auto",
        help="默认 auto：给了 path 就当文本读，没给就去找 Apple Books",
    )
    ap.add_argument("--book", help="只导出书名包含该关键词的书")
    ap.add_argument("--style", help="只要某个颜色的划线（Apple Books：黄/蓝/绿/粉/紫/下划线）")
    ap.add_argument("--list", action="store_true", help="只列出有划线的书和条数")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("-o", "--out", help="输出文件，默认打印到 stdout")
    args = ap.parse_args(argv)

    if args.source in ("kindle", "text") and not args.path:
        print("--source kindle/text 需要给出文件路径。", file=sys.stderr)
        return 1

    try:
        clippings = load(args)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    books = group_by_book(clippings)

    if args.book:
        needle = args.book.lower()
        books = OrderedDict((k, v) for k, v in books.items() if needle in k.lower())
    if args.style:
        for b in books.values():
            b.clippings = [c for c in b.clippings if c.style == args.style]
        books = OrderedDict((k, v) for k, v in books.items() if v.clippings)
    if not books:
        print("没有匹配到任何划线。", file=sys.stderr)
        return 1

    if args.list:
        for b in books.values():
            n_h = sum(1 for c in b.clippings if c.kind == "highlight")
            n_n = sum(1 for c in b.clippings if c.note)
            who = f" — {b.author}" if b.author else ""
            print(f"{len(b.clippings):5d} 条 (划线 {n_h} / 批注 {n_n})  {b.title}{who}")
        return 0

    if args.format == "json":
        text = json.dumps(
            [
                {"book": b.title, "author": b.author,
                 "clippings": [c.to_dict() for c in b.clippings]}
                for b in books.values()
            ],
            ensure_ascii=False, indent=2,
        )
    else:
        text = render_markdown(books)

    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        total = sum(len(b.clippings) for b in books.values())
        print(f"已写入 {args.out}：{len(books)} 本书，{total} 条。", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
