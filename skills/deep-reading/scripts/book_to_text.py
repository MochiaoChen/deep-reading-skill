#!/usr/bin/env python3
"""把 EPUB / PDF 拆成按章分段的纯文本，供强力研读第二遍逐章记笔记。

用法:
  python3 book_to_text.py 书.epub --toc                # 先看目录结构
  python3 book_to_text.py 书.epub -o 书.md             # 合成一个 markdown
  python3 book_to_text.py 书.pdf  --split ./chapters   # 一章一个文件（推荐）
  python3 book_to_text.py 书.epub --format json

EPUB 用标准库解析。PDF 优先调用系统里的 pdftotext / mutool / pypdf / pdfminer /
PyMuPDF，一个都没有时退回内置的纯标准库提取器（zlib + ToUnicode CMap，能处理
大多数含嵌入文本的 PDF，包括中文 CID 字体；扫描件除外）。

「读一章，记一章」——所以默认按章切分，不要一次把整本书塞给模型。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------- 数据结构

@dataclass
class Section:
    title: str
    text: str

    @property
    def chars(self) -> int:
        return len(re.sub(r"\s", "", self.text))


@dataclass
class Book:
    title: str = ""
    author: str = ""
    source: str = ""
    confidence: float = 1.0
    sections: list = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(s.chars for s in self.sections)


# ---------------------------------------------------------------- EPUB

BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "li", "tr", "br",
    "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "pre", "td",
}
DROP_TAGS = {"script", "style", "head", "svg", "nav"}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.headings = []
        self._drop = 0
        self._heading = None

    def handle_starttag(self, tag, attrs):
        if tag in DROP_TAGS:
            self._drop += 1
            return
        if self._drop:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag in {"li"}:
            self.parts.append("- ")
        if re.fullmatch(r"h[1-6]", tag):
            self._heading = ""

    def handle_endtag(self, tag):
        if tag in DROP_TAGS:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop:
            return
        if re.fullmatch(r"h[1-6]", tag):
            if self._heading and self._heading.strip():
                self.headings.append(self._heading.strip())
            self._heading = None
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._drop:
            return
        self.parts.append(data)
        if self._heading is not None:
            self._heading += data

    def result(self) -> str:
        return "".join(self.parts)


def html_to_text(raw: str) -> tuple[str, list]:
    parser = _HTMLText()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    text = html.unescape(parser.result())
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), parser.headings


def _ns_strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def epub_to_book(path: Path) -> Book:
    book = Book(source="epub")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        opf_path = None
        if "META-INF/container.xml" in names:
            root = ET.fromstring(zf.read("META-INF/container.xml"))
            for el in root.iter():
                if _ns_strip(el.tag) == "rootfile" and el.get("full-path"):
                    opf_path = el.get("full-path")
                    break
        if not opf_path:
            opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
        if not opf_path:
            raise ValueError("EPUB 里找不到 .opf，文件可能已损坏")

        base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
        opf = ET.fromstring(zf.read(opf_path))

        manifest, spine = {}, []
        for el in opf.iter():
            name = _ns_strip(el.tag)
            if name == "item":
                manifest[el.get("id")] = (el.get("href", ""), el.get("media-type", ""))
            elif name == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
            elif name == "title" and not book.title:
                book.title = (el.text or "").strip()
            elif name == "creator" and not book.author:
                book.author = (el.text or "").strip()

        def resolve(href: str) -> str:
            href = href.split("#")[0]
            full = f"{base}/{href}" if base else href
            parts = []
            for seg in full.split("/"):
                if seg == "..":
                    parts and parts.pop()
                elif seg not in ("", "."):
                    parts.append(seg)
            return "/".join(parts)

        toc = epub_toc(zf, manifest, resolve, names)

        for idx, ref in enumerate(spine, 1):
            href, mime = manifest.get(ref, ("", ""))
            if not href or ("html" not in mime and not href.lower().endswith((".html", ".xhtml", ".htm"))):
                continue
            target = resolve(href)
            if target not in names:
                continue
            raw = zf.read(target).decode("utf-8", errors="replace")
            text, headings = html_to_text(raw)
            if not text.strip():
                continue
            title = (
                toc.get(target)
                or (headings[0] if headings else "")
                or f"第 {idx} 节"
            )
            book.sections.append(Section(title, text))

    book.title = book.title or path.stem
    return book


def epub_toc(zf, manifest, resolve, names) -> dict:
    """从 nav.xhtml 或 toc.ncx 取 href -> 章节标题。"""
    mapping = {}
    candidates = [
        resolve(h) for h, m in manifest.values()
        if "ncx" in m or h.lower().endswith((".ncx", "nav.xhtml"))
    ]
    for cand in candidates:
        if cand not in names:
            continue
        try:
            root = ET.fromstring(zf.read(cand))
        except ET.ParseError:
            continue
        for el in root.iter():
            name = _ns_strip(el.tag)
            if name == "navPoint":
                label = "".join(
                    (t.text or "") for t in el.iter() if _ns_strip(t.tag) == "text"
                ).strip()
                src = next(
                    (c.get("src") for c in el.iter() if _ns_strip(c.tag) == "content"), None
                )
                if label and src:
                    mapping.setdefault(resolve(src), label)
            elif name == "a" and el.get("href"):
                label = "".join(el.itertext()).strip()
                if label:
                    mapping.setdefault(resolve(el.get("href")), label)
    return mapping


# ---------------------------------------------------------------- PDF：外部后端

def _run(cmd, **kw) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=600, **kw)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def pdf_pages_external(path: Path):
    """返回 (backend_name, [page_text, ...])，没有可用后端返回 None。"""
    if shutil.which("pdftotext"):
        text = _run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"])
        if text:
            return "pdftotext", text.split("\f")
    if shutil.which("mutool"):
        text = _run(["mutool", "draw", "-F", "txt", str(path)])
        if text:
            return "mutool", text.split("\f")
    for mod, fn in (
        ("pypdf", lambda m: [p.extract_text() or "" for p in m.PdfReader(str(path)).pages]),
        ("PyPDF2", lambda m: [p.extract_text() or "" for p in m.PdfReader(str(path)).pages]),
        ("fitz", lambda m: [pg.get_text() for pg in m.open(str(path))]),
    ):
        try:
            import importlib
            module = importlib.import_module(mod)
            pages = fn(module)
            if any(p.strip() for p in pages):
                return mod, pages
        except Exception:
            continue
    try:
        from pdfminer.high_level import extract_text as _pm
        text = _pm(str(path))
        if text.strip():
            return "pdfminer", text.split("\f")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- PDF：内置提取器

OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
STREAM_RE = re.compile(rb"stream\r?\n?(.*?)\r?\n?endstream", re.S)
HEXSTR_RE = re.compile(rb"<([0-9A-Fa-f\s]*)>")


def _inflate(data: bytes) -> bytes:
    for attempt in (data, data.lstrip(b"\r\n")):
        try:
            return zlib.decompress(attempt)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(attempt)
            except zlib.error:
                continue
    return b""


class MiniPDF:
    """够用就好的 PDF 解析器：只为把文字捞出来，不追求规范完备。"""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.objects: dict[int, bytes] = {}
        self.streams: dict[int, bytes] = {}
        self._scan()
        self._expand_object_streams()

    def _scan(self):
        for m in OBJ_RE.finditer(self.raw):
            num = int(m.group(1))
            body = m.group(3)
            sm = STREAM_RE.search(body)
            if sm:
                self.objects[num] = body[: sm.start()]
                self.streams[num] = sm.group(1)
            else:
                self.objects[num] = body

    def _expand_object_streams(self):
        for num, header in list(self.objects.items()):
            if b"/ObjStm" not in header:
                continue
            data = self.stream_data(num)
            if not data:
                continue
            n = self._int(header, b"/N")
            first = self._int(header, b"/First")
            if not n or first is None:
                continue
            try:
                nums = data[:first].split()
                pairs = [
                    (int(nums[i]), int(nums[i + 1])) for i in range(0, min(len(nums), 2 * n), 2)
                ]
            except (ValueError, IndexError):
                continue
            for i, (onum, off) in enumerate(pairs):
                end = pairs[i + 1][1] + first if i + 1 < len(pairs) else len(data)
                self.objects.setdefault(onum, data[first + off : end])

    @staticmethod
    def _int(blob: bytes, key: bytes):
        m = re.search(re.escape(key) + rb"\s+(\d+)", blob)
        return int(m.group(1)) if m else None

    def stream_data(self, num: int) -> bytes:
        raw = self.streams.get(num)
        if raw is None:
            return b""
        header = self.objects.get(num, b"")
        if b"FlateDecode" in header:
            return _inflate(raw)
        if b"/Filter" in header:
            return b""  # DCT/JPX/LZW 等：不是文字
        return raw

    def refs(self, blob: bytes, key: bytes) -> list:
        m = re.search(re.escape(key) + rb"\s*(\[.*?\]|\d+\s+\d+\s+R)", blob, re.S)
        if not m:
            return []
        return [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1))]

    def page_objects(self) -> list:
        catalog = next(
            (n for n, b in self.objects.items() if b"/Type" in b and b"/Catalog" in b), None
        )
        ordered, seen = [], set()

        def walk(num: int, depth: int = 0):
            if num in seen or depth > 60:
                return
            seen.add(num)
            body = self.objects.get(num, b"")
            if b"/Type" in body and b"/Page" in body and b"/Pages" not in body:
                ordered.append(num)
                return
            for kid in self.refs(body, b"/Kids"):
                walk(kid, depth + 1)

        if catalog is not None:
            for pages in self.refs(self.objects[catalog], b"/Pages"):
                walk(pages)
        if not ordered:
            ordered = sorted(
                n for n, b in self.objects.items()
                if b"/Type" in b and re.search(rb"/Type\s*/Page\b", b)
            )
        return ordered

    # ---- 字体与 ToUnicode

    def font_map(self, page_num: int) -> dict:
        """字体资源名 -> (unicode_map, code_byte_width)"""
        body = self.objects.get(page_num, b"")
        res_blob = b""
        m = re.search(rb"/Resources\s*(<<.*?>>)", body, re.S)
        if m:
            res_blob = m.group(1)
        else:
            for r in self.refs(body, b"/Resources"):
                res_blob = self.objects.get(r, b"")
                break
        if not res_blob:
            for parent in self.refs(body, b"/Parent"):
                pbody = self.objects.get(parent, b"")
                for r in self.refs(pbody, b"/Resources"):
                    res_blob = self.objects.get(r, b"")
                m2 = re.search(rb"/Resources\s*(<<.*?>>)", pbody, re.S)
                if m2:
                    res_blob = m2.group(1)

        fonts = {}
        fm = re.search(rb"/Font\s*(<<.*?>>)", res_blob, re.S)
        font_blob = fm.group(1) if fm else b""
        if not font_blob:
            for r in self.refs(res_blob, b"/Font"):
                font_blob = self.objects.get(r, b"")
                break
        for name, num in re.findall(rb"/([A-Za-z0-9_.+\-]+)\s+(\d+)\s+\d+\s+R", font_blob):
            fonts[name.decode("latin-1")] = self._font_unicode(int(num))
        return fonts

    def _font_unicode(self, num: int):
        body = self.objects.get(num, b"")
        two_byte = b"/Type0" in body or b"Identity" in body
        umap = {}
        for tu in self.refs(body, b"/ToUnicode"):
            umap = parse_cmap(self.stream_data(tu))
            break
        if not umap:
            for desc in self.refs(body, b"/DescendantFonts"):
                dbody = self.objects.get(desc, b"")
                if dbody:
                    two_byte = True
        width = 2 if (two_byte or (umap and max(len(k) for k in umap) > 2)) else 1
        return umap, width


def parse_cmap(data: bytes) -> dict:
    """解析 ToUnicode CMap 的 bfchar / bfrange，返回 hex码 -> unicode 串。"""
    if not data:
        return {}
    out = {}

    def to_str(hx: bytes) -> str:
        h = re.sub(rb"\s", b"", hx)
        if len(h) % 4:
            h = h + b"0" * (4 - len(h) % 4)
        try:
            return bytes.fromhex(h.decode()).decode("utf-16-be", errors="ignore")
        except ValueError:
            return ""

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        items = HEXSTR_RE.findall(block)
        for i in range(0, len(items) - 1, 2):
            code = re.sub(rb"\s", b"", items[i]).decode().lower()
            out[code] = to_str(items[i + 1])

    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        # 形式一: <lo> <hi> <dst>
        for lo, hi, dst in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            width = len(lo)
            start, end = int(lo, 16), int(hi, 16)
            if end - start > 65535:
                continue
            base = to_str(dst)
            for i, code in enumerate(range(start, end + 1)):
                if base:
                    out[format(code, f"0{width}x")] = base[:-1] + chr(ord(base[-1]) + i)
        # 形式二: <lo> <hi> [<d1> <d2> ...]
        for lo, hi, arr in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S
        ):
            width = len(lo)
            dsts = HEXSTR_RE.findall(arr)
            start = int(lo, 16)
            for i, d in enumerate(dsts):
                out[format(start + i, f"0{width}x")] = to_str(d)
    return out


TEXT_OPS_RE = re.compile(
    rb"(?P<hex><[0-9A-Fa-f\s]*>)|(?P<paren>\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\))"
    rb"|(?P<font>/([A-Za-z0-9_.+\-]+)\s+[\d.]+\s+Tf)"
    rb"|(?P<op>\bT[Jj\*dD]|\bTD\b|\bET\b|'|\")",
    re.S,
)


def _decode_string(raw: bytes) -> bytes:
    """PDF 字面串 (...) 去转义。"""
    out, i = bytearray(), 0
    body = raw[1:-1]
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):
            nxt = body[i + 1]
            mapping = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
            if nxt in mapping:
                out.append(mapping[nxt]); i += 2
            elif 0x30 <= nxt <= 0x37:
                oct_digits = body[i + 1 : i + 4]
                m = re.match(rb"[0-7]{1,3}", oct_digits)
                out.append(int(m.group(), 8) & 0xFF); i += 1 + len(m.group())
            elif nxt in (10, 13):
                i += 2
            else:
                out.append(nxt); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


def render_codes(data: bytes, umap: dict, width: int) -> str:
    if not umap:
        if width == 2:
            return data.decode("utf-16-be", errors="ignore")
        return data.decode("latin-1", errors="ignore")
    chars = []
    step = width
    for i in range(0, len(data) - step + 1, step):
        code = data[i : i + step].hex()
        chars.append(umap.get(code, umap.get(code.lstrip("0") or "0", "")))
    return "".join(chars)


def extract_page_text(content: bytes, fonts: dict) -> str:
    umap, width = {}, 1
    pieces = []
    for m in TEXT_OPS_RE.finditer(content):
        if m.group("font"):
            umap, width = fonts.get(m.group(4).decode("latin-1"), ({}, 1))
        elif m.group("hex"):
            hx = re.sub(rb"\s", b"", m.group("hex")[1:-1])
            if len(hx) % 2:
                hx += b"0"
            try:
                pieces.append(render_codes(bytes.fromhex(hx.decode()), umap, width))
            except ValueError:
                pass
        elif m.group("paren"):
            pieces.append(render_codes(_decode_string(m.group("paren")), umap, width))
        elif m.group("op") in (b"Td", b"TD", b"T*", b"'", b'"', b"ET"):
            pieces.append("\n")
    text = "".join(pieces)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# 只把「正常行文里会出现的字符」算作可读：CJK、拉丁字母数字、基本标点。
# 字体缺 ToUnicode 时，码位会按 latin-1 硬解成一堆奇符号，这一项立刻掉下来。
LEGIBLE_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef"
    r"0-9A-Za-z .,;:!?'\"()\-\u2014\u2013\u2026\n]"
)
MIN_CONFIDENCE = 0.85


def text_confidence(text: str) -> float:
    """可读字符占比。字体缺 ToUnicode 时会吐出乱码，用这个把它挡住。"""
    sample = re.sub(r"\s", "", text)[:20000]
    if not sample:
        return 0.0
    return sum(1 for ch in sample if LEGIBLE_RE.match(ch)) / len(sample)


def pdf_pages_builtin(path: Path) -> list:
    pdf = MiniPDF(path.read_bytes())
    pages = []
    for num in pdf.page_objects():
        body = pdf.objects.get(num, b"")
        chunks = [pdf.stream_data(c) for c in pdf.refs(body, b"/Contents")]
        content = b"\n".join(c for c in chunks if c)
        if not content:
            continue
        pages.append(extract_page_text(content, pdf.font_map(num)))
    return pages


# ---------------------------------------------------------------- PDF -> Book

CHAPTER_RE = re.compile(
    r"^[ \t]*(?:第\s*[0-9一二三四五六七八九十百]+\s*[章节篇回]"
    r"|Chapter\s+[0-9IVXLC]+|CHAPTER\s+[0-9IVXLC]+"
    r"|(?<![\d.])[0-9]{1,2}\s*[.、][ \t]*[^\d\s%][^\n]{1,38})[ \t]*[^\n]{0,40}$",
    re.M,
)

MIN_SECTION_CHARS = 200


def looks_like_heading(text: str) -> bool:
    """挡住图表里的 "5.0%" 这类假标题。"""
    if "%" in text or len(text) > 60:
        return False
    return len(re.findall(r"[一-鿿A-Za-z]", text)) >= 2


def merge_tiny(sections: list) -> list:
    """太短的"章"多半是误判的标题，并回上一节。"""
    merged = []
    for s in sections:
        if merged and s.chars < MIN_SECTION_CHARS:
            prev = merged[-1]
            merged[-1] = Section(prev.title, f"{prev.text}\n\n{s.title}\n{s.text}".strip())
        else:
            merged.append(s)
    return merged


def pdf_to_book(path: Path, force_builtin: bool = False, allow_garbled: bool = False) -> Book:
    backend, pages = None, None
    if not force_builtin:
        got = pdf_pages_external(path)
        if got:
            backend, pages = got
    if pages is None:
        backend, pages = "builtin", pdf_pages_builtin(path)

    book = Book(title=path.stem, source=f"pdf/{backend}")
    joined = "\n\f\n".join(pages)
    if not re.sub(r"\s", "", joined):
        raise ValueError(
            "没有提取到任何文字。这多半是扫描版 PDF（图片），需要 OCR；"
            "或者直接把 PDF 交给 Claude 的 Read 工具按页读。"
        )

    confidence = text_confidence(joined)
    book.confidence = confidence
    if backend == "builtin" and confidence < MIN_CONFIDENCE and not allow_garbled:
        raise ValueError(
            f"提取出来的是乱码（可读字符仅 {confidence:.0%}）。\n"
            "原因：这个 PDF 的嵌入字体没有 ToUnicode 映射表，纯标准库还原不出字符。\n"
            "三条出路，任选其一——\n"
            "  1) 装一个真正的 PDF 引擎（最省事）：\n"
            "       brew install poppler        # 提供 pdftotext\n"
            "       pip install pypdf           # 或 pdfminer.six / PyMuPDF\n"
            "     装完重跑本命令即可，脚本会自动优先用它。\n"
            "  2) 把 PDF 直接交给 Claude 的 Read 工具按页读（无需安装任何东西）。\n"
            "  3) 确实想看这堆乱码，加 --force。"
        )

    # 用章标题切分；切不出来就按页分组
    marks = [(m.start(), m.group().strip()) for m in CHAPTER_RE.finditer(joined)]
    marks = [(pos, t) for pos, t in marks if looks_like_heading(t)]
    if len(marks) >= 3:
        if marks[0][0] > 0:
            marks.insert(0, (0, "前言 / 卷首"))
        for i, (pos, title) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(joined)
            body = joined[pos:end].replace("\f", "\n").strip()
            if re.sub(r"\s", "", body):
                book.sections.append(Section(title, body))
        book.sections = merge_tiny(book.sections)
    else:
        group = max(1, len(pages) // 20) if len(pages) > 40 else 10
        for i in range(0, len(pages), group):
            chunk = "\n\n".join(pages[i : i + group]).strip()
            if re.sub(r"\s", "", chunk):
                label = f"第 {i + 1}–{min(i + group, len(pages))} 页"
                book.sections.append(Section(label, chunk))
    return book


# ---------------------------------------------------------------- 输出

def render_markdown(book: Book) -> str:
    lines = [f"# {book.title}"]
    if book.author:
        lines.append(f"*{book.author}*")
    lines += [
        "",
        f"> 由 book_to_text.py 提取（{book.source}）：{len(book.sections)} 节，约 {book.chars} 字。",
        "> 这是原文，不是笔记。下一步走 SKILL.md 的第二遍：读一章，记一章。",
        "",
    ]
    for s in book.sections:
        lines += [f"## {s.title}", "", s.text, ""]
    return "\n".join(lines).rstrip() + "\n"


def safe_name(text: str, idx: int) -> str:
    cleaned = re.sub(r"[^\w一-鿿 \-]+", "", text).strip()[:40] or "section"
    return f"{idx:02d}-{cleaned}.md"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="EPUB / PDF → 按章分节的纯文本")
    ap.add_argument("path", help="EPUB 或 PDF 文件")
    ap.add_argument("-o", "--out", help="写入单个 markdown 文件")
    ap.add_argument("--split", metavar="DIR", help="一章一个文件写入该目录（推荐）")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--toc", action="store_true", help="只列出章节结构和字数")
    ap.add_argument("--builtin", action="store_true", help="PDF 强制使用内置提取器")
    ap.add_argument("--force", action="store_true", help="即使提取结果疑似乱码也照样输出")
    args = ap.parse_args(argv)

    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 1

    suffix = path.suffix.lower()
    try:
        if suffix == ".epub":
            book = epub_to_book(path)
        elif suffix == ".pdf":
            book = pdf_to_book(path, force_builtin=args.builtin, allow_garbled=args.force)
        else:
            print(f"只支持 .epub 和 .pdf，收到 {suffix}", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"提取失败: {exc}", file=sys.stderr)
        return 1

    if not book.sections:
        print("没有解析出任何正文。", file=sys.stderr)
        return 1

    if args.toc:
        print(f"{book.title}" + (f" — {book.author}" if book.author else ""))
        print(f"来源 {book.source}，共 {len(book.sections)} 节，约 {book.chars} 字\n")
        for i, s in enumerate(book.sections, 1):
            print(f"{i:3d}. {s.title}  ({s.chars} 字)")
        return 0

    if args.split:
        out_dir = Path(args.split).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, s in enumerate(book.sections, 1):
            (out_dir / safe_name(s.title, i)).write_text(
                f"# {s.title}\n\n{s.text}\n", encoding="utf-8"
            )
        print(f"已写入 {out_dir}：{len(book.sections)} 节。", file=sys.stderr)
        return 0

    if args.format == "json":
        text = json.dumps(
            {
                "title": book.title,
                "author": book.author,
                "source": book.source,
                "sections": [{"title": s.title, "text": s.text} for s in book.sections],
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        text = render_markdown(book)

    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}：{len(book.sections)} 节，约 {book.chars} 字。", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
