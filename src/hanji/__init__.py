"""hanji — 한글(HWP 5.0 / HWPX) 문서를 텍스트·마크다운·HTML·RAG 청크로.

    >>> import hanji
    >>> doc = hanji.load("보고서.hwp")
    >>> print(doc.to_markdown())

Pure Python. Needs only ``olefile`` (for the binary .hwp container).
본 제품은 한글과컴퓨터의 한글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
"""

from __future__ import annotations

import html
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from os import PathLike
from typing import BinaryIO, Iterator, Literal, Union
from xml.etree import ElementTree as ET

__version__ = "0.1.0"
__all__ = [
    "load",
    "loads",
    "detect",
    "Document",
    "Paragraph",
    "Table",
    "Cell",
    "Note",
    "Chunk",
    "HanjiError",
]

Source = Union[str, PathLike, bytes, BinaryIO]


# --------------------------------------------------------------------------- #
# 데이터 모델
# --------------------------------------------------------------------------- #


@dataclass
class Paragraph:
    text: str
    style: str | None = None
    level: int | None = None  # 개요 1~7 → 1~7, 나머지는 None

    kind: Literal["paragraph"] = "paragraph"


@dataclass
class Cell:
    row: int
    col: int
    text: str
    rowspan: int = 1
    colspan: int = 1


@dataclass
class Table:
    rows: int
    cols: int
    cells: list[Cell] = field(default_factory=list)

    kind: Literal["table"] = "table"

    def grid(self) -> list[list[str]]:
        """병합된 셀은 왼쪽 위 칸에만 글자를 두고 나머지는 빈 문자열로 채운 2차원 표."""
        nrows = max([self.rows] + [c.row + 1 for c in self.cells])
        ncols = max([self.cols] + [c.col + 1 for c in self.cells])
        out = [[""] * ncols for _ in range(nrows)]
        for c in self.cells:
            out[c.row][c.col] = c.text
        return out


Block = Union[Paragraph, Table]


@dataclass
class Note:
    number: int
    kind: Literal["footnote", "endnote"]
    text: str


@dataclass
class Chunk:
    text: str
    section: int
    start: int  # 섹션 안의 블록 번호 (포함)
    end: int  # (포함)
    headings: list[str]

    @property
    def id(self) -> str:
        return f"s{self.section}b{self.start}-{self.end}"


@dataclass
class Document:
    format: Literal["hwp", "hwpx"]
    version: str
    title: str | None
    author: str | None
    sections: list[list[Block]]
    notes: list[Note]

    # -- 편의 속성 ---------------------------------------------------------

    @property
    def blocks(self) -> list[Block]:
        return [b for s in self.sections for b in s]

    @property
    def tables(self) -> list[Table]:
        return [b for b in self.blocks if isinstance(b, Table)]

    # -- 출력 --------------------------------------------------------------

    def to_text(self) -> str:
        parts = []
        for b in self.blocks:
            if isinstance(b, Paragraph):
                parts.append(b.text)
            else:
                parts.append("\n".join("\t".join(r) for r in b.grid()))
        return "\n\n".join(parts)

    def to_markdown(self) -> str:
        parts = [_block_md(b) for b in self.blocks]
        if self.notes:
            parts.append("\n".join(f"[^{n.number}]: {_one_line(n.text)}" for n in self.notes))
        return "\n\n".join(p for p in parts if p)

    def to_html(self) -> str:
        return "\n".join(_block_html(b) for b in self.blocks)

    def chunks(self, size: int = 800, overlap: int = 80) -> list[Chunk]:
        """RAG용 청크. 제목이 나오면 새 청크를 시작하고, 각 청크에 상위 제목 경로를 붙입니다."""
        return list(_chunk(self, size, overlap))

    def __repr__(self) -> str:  # 긴 본문을 찍지 않도록
        n = sum(len(s) for s in self.sections)
        return f"<hanji.Document {self.format} v{self.version} sections={len(self.sections)} blocks={n} notes={len(self.notes)}>"


class HanjiError(Exception):
    """읽을 수 없는 문서. ``reason`` 으로 원인을 구분할 수 있습니다."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason  # unknown-format | hwp3 | encrypted | distribution | broken


# --------------------------------------------------------------------------- #
# 진입점
# --------------------------------------------------------------------------- #


def _read_all(src: Source) -> bytes:
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src)
    if isinstance(src, (str, PathLike)):
        with open(src, "rb") as f:
            return f.read()
    return src.read()


def detect(data: bytes) -> str:
    """앞부분 몇 바이트로 형식 판별: 'hwp' | 'hwpx' | 'hwp3' | 'unknown'."""
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return "hwp"
    if data[:2] == b"PK":
        return "hwpx"
    if data[:17] == b"HWP Document File":
        return "hwp3"
    return "unknown"


def load(src: Source, *, notes: bool = True, equations: bool = True) -> Document:
    """파일 경로, bytes, 또는 열린 바이너리 파일을 받아 문서를 읽습니다."""
    return loads(_read_all(src), notes=notes, equations=equations)


def loads(data: bytes, *, notes: bool = True, equations: bool = True) -> Document:
    kind = detect(data)
    opts = _Opts(notes=notes, equations=equations)
    if kind == "hwp":
        return _HwpReader(data, opts).read()
    if kind == "hwpx":
        return _HwpxReader(data, opts).read()
    if kind == "hwp3":
        raise HanjiError("hwp3", "HWP 3.x 문서는 지원하지 않아요. 한글에서 HWP 5.0 이상으로 다시 저장해 주세요.")
    raise HanjiError("unknown-format", "HWP/HWPX 파일이 아닙니다.")


@dataclass
class _Opts:
    notes: bool
    equations: bool


# --------------------------------------------------------------------------- #
# 공통
# --------------------------------------------------------------------------- #

_OUTLINE = re.compile(r"^(?:개요|Outline)\s*(\d)", re.I)


def _level(style: str | None) -> int | None:
    m = _OUTLINE.match(style.strip()) if style else None
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 7 else None


def _clean(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return s.rstrip()


class _Collector:
    """문단 텍스트를 모으다가 표·글상자 같은 블록이 끼어들면 앞부분을 먼저 문단으로 내보냅니다."""

    def __init__(self, out: list[Block], style: str | None):
        self.out, self.style, self.buf = out, style, []

    def add(self, s: str) -> None:
        self.buf.append(s)

    def flush(self) -> None:
        text = _clean("".join(self.buf))
        self.buf = []
        if text.strip():
            self.out.append(Paragraph(text, self.style, _level(self.style)))

    def push(self, blocks: list[Block]) -> None:
        self.flush()
        self.out.extend(blocks)


def _plain(blocks: list[Block]) -> str:
    lines = []
    for b in blocks:
        if isinstance(b, Paragraph):
            lines.append(b.text)
        else:
            lines.extend(" | ".join(r) for r in b.grid())
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# HWP 5.0 (OLE 복합 파일 + 레코드)
# --------------------------------------------------------------------------- #

_T_STYLE = 0x10 + 26
_T_PARA_HEADER = 0x10 + 50
_T_PARA_TEXT = 0x10 + 51
_T_CTRL_HEADER = 0x10 + 55
_T_LIST_HEADER = 0x10 + 56
_T_TABLE = 0x10 + 61
_T_EQEDIT = 0x10 + 72


def _ctrl(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


_C_TABLE = _ctrl("tbl ")
_C_GSO = _ctrl("gso ")
_C_EQ = _ctrl("eqed")
_C_FOOTNOTE = _ctrl("fn  ")
_C_ENDNOTE = _ctrl("en  ")
_C_HEADER = _ctrl("head")
_C_FOOTER = _ctrl("foot")

# 8 WCHAR(16바이트)를 차지하는 제어 문자. EXTENDED는 CTRL_HEADER 레코드와 짝을 이룹니다.
_EXTENDED = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
_INLINE = {4, 5, 6, 7, 8, 9, 19, 20}


@dataclass
class _Rec:
    tag: int
    level: int
    data: bytes
    children: list["_Rec"] = field(default_factory=list)


def _records(buf: bytes) -> list[_Rec]:
    root = _Rec(-1, -1, b"")
    stack = [root]
    pos, n = 0, len(buf)
    while pos + 4 <= n:
        (h,) = struct.unpack_from("<I", buf, pos)
        pos += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, (h >> 20) & 0xFFF
        if size == 0xFFF:
            (size,) = struct.unpack_from("<I", buf, pos)
            pos += 4
        if pos + size > n:
            raise HanjiError("broken", "레코드 길이가 스트림을 넘어갑니다.")
        rec = _Rec(tag, level, buf[pos : pos + size])
        pos += size
        while len(stack) > 1 and stack[-1].level >= level:
            stack.pop()
        stack[-1].children.append(rec)
        stack.append(rec)
    return root.children


def _u16(b: bytes, o: int) -> int:
    return b[o] | (b[o + 1] << 8)


def _utf16(b: bytes, o: int, chars: int) -> str:
    return b[o : o + chars * 2].decode("utf-16-le", errors="replace")


def _para_tokens(data: bytes) -> Iterator[str | int]:
    """PARA_TEXT를 글자 조각(str)과 컨트롤 순번(int)으로 나눕니다."""
    text: list[str] = []
    idx = 0
    p = 0
    while p + 1 < len(data):
        c = _u16(data, p)
        if c >= 32:
            # 서로게이트 쌍 포함, 연속된 일반 글자를 한 번에 디코딩
            q = p
            while q + 1 < len(data) and _u16(data, q) >= 32:
                q += 2
            text.append(data[p:q].decode("utf-16-le", errors="replace"))
            p = q
        elif c in _EXTENDED:
            if text:
                yield "".join(text)
                text = []
            yield idx
            idx += 1
            p += 16
        elif c in _INLINE:
            if c == 9:
                text.append("\t")
            p += 16
        else:
            if c == 10:
                text.append("\n")
            elif c == 24:
                text.append("-")
            elif c in (30, 31):
                text.append(" ")
            p += 2
    if text:
        yield "".join(text)


class _HwpReader:
    def __init__(self, data: bytes, opts: _Opts):
        try:
            import olefile
        except ImportError as e:  # pragma: no cover
            raise ImportError("HWP 5.0 파일을 읽으려면 olefile 이 필요해요: pip install olefile") from e
        try:
            self.ole = olefile.OleFileIO(io.BytesIO(data))
        except Exception as e:
            raise HanjiError("broken", "OLE 컨테이너를 열 수 없습니다.") from e
        self.opts = opts
        self.styles: list[str] = []
        self.notes: list[Note] = []

    def _stream(self, path: str) -> bytes | None:
        return self.ole.openstream(path).read() if self.ole.exists(path) else None

    def read(self) -> Document:
        header = self._stream("FileHeader")
        if not header or header[:17] != b"HWP Document File":
            raise HanjiError("unknown-format", "FileHeader 서명이 없습니다.")
        v, flags = struct.unpack_from("<II", header, 32)
        version = f"{v >> 24 & 0xFF}.{v >> 16 & 0xFF}.{v >> 8 & 0xFF}.{v & 0xFF}"
        if flags & 0x2:
            raise HanjiError("encrypted", "암호가 걸린 문서입니다.")
        if flags & 0x4:
            raise HanjiError("distribution", "배포용 문서는 본문이 암호화되어 있어 읽을 수 없습니다.")
        compressed = bool(flags & 0x1)

        def load(path: str) -> bytes | None:
            raw = self._stream(path)
            if raw is None or not compressed:
                return raw
            try:
                return zlib.decompress(raw, -15)
            except zlib.error as e:
                raise HanjiError("broken", f"{path} 압축을 풀 수 없습니다.") from e

        info = load("DocInfo")
        if info:
            self.styles = self._styles(_records(info))

        sections = []
        i = 0
        while (body := load(f"BodyText/Section{i}")) is not None:
            sections.append(self._collect(_records(body)))
            i += 1
        if not sections:
            raise HanjiError("broken", "BodyText 섹션이 없습니다.")

        title, author = self._summary()
        self.ole.close()
        return Document("hwp", version, title, author, sections, self.notes)

    def _styles(self, recs: list[_Rec]) -> list[str]:
        out: list[str] = []

        def walk(rs: list[_Rec]) -> None:
            for r in rs:
                if r.tag == _T_STYLE and len(r.data) >= 2:
                    out.append(_utf16(r.data, 2, _u16(r.data, 0)))
                walk(r.children)

        walk(recs)
        return out

    def _summary(self) -> tuple[str | None, str | None]:
        """\x05HwpSummaryInformation 에서 제목(PID 2)과 작성자(PID 4)를 읽습니다.

        olefile.getproperties() 는 VT_LPWSTR 길이를 바이트로 해석해서 값 뒤에 쓰레기가 붙기 때문에 직접 읽습니다.
        """
        d = self._stream("\x05HwpSummaryInformation")
        if not d or len(d) < 48:
            return None, None
        found: dict[int, str] = {}
        try:
            (off,) = struct.unpack_from("<I", d, 44)
            (count,) = struct.unpack_from("<I", d, off + 4)
            for i in range(count):
                pid, rel = struct.unpack_from("<II", d, off + 8 + i * 8)
                at = off + rel
                vtype, nchars = struct.unpack_from("<II", d, at)
                if pid in (2, 4) and vtype == 0x1F:  # VT_LPWSTR, 길이는 글자 수(널 포함)
                    found[pid] = _utf16(d, at + 8, nchars).split("\x00", 1)[0].strip()
        except struct.error:
            pass
        return found.get(2) or None, found.get(4) or None

    def _collect(self, recs: list[_Rec]) -> list[Block]:
        out: list[Block] = []
        for r in recs:
            if r.tag == _T_PARA_HEADER:
                self._paragraph(r, out)
            elif r.tag == _T_CTRL_HEADER and len(r.data) >= 4:
                if struct.unpack_from("<I", r.data)[0] in (_C_HEADER, _C_FOOTER):
                    continue
                out.extend(self._collect(r.children))
            else:
                out.extend(self._collect(r.children))
        return out

    def _paragraph(self, rec: _Rec, out: list[Block]) -> None:
        style_id = rec.data[10] if len(rec.data) > 10 else 0
        style = self.styles[style_id] if style_id < len(self.styles) else None
        col = _Collector(out, style)
        text_rec = next((c for c in rec.children if c.tag == _T_PARA_TEXT), None)
        ctrls = [c for c in rec.children if c.tag == _T_CTRL_HEADER]

        for tok in _para_tokens(text_rec.data) if text_rec else ():
            if isinstance(tok, str):
                col.add(tok)
                continue
            if tok >= len(ctrls) or len(ctrls[tok].data) < 4:
                continue
            ctrl = ctrls[tok]
            (cid,) = struct.unpack_from("<I", ctrl.data)
            if cid == _C_TABLE:
                t = self._table(ctrl)
                if t:
                    col.push([t])
            elif cid in (_C_FOOTNOTE, _C_ENDNOTE) and self.opts.notes:
                note = Note(len(self.notes) + 1, "footnote" if cid == _C_FOOTNOTE else "endnote", "")
                self.notes.append(note)
                note.text = _plain(self._collect(ctrl.children))
                col.add(f"[^{note.number}]")
            elif cid == _C_EQ and self.opts.equations:
                eq = next((c for c in ctrl.children if c.tag == _T_EQEDIT), None)
                if eq and len(eq.data) >= 6:
                    script = _utf16(eq.data, 6, _u16(eq.data, 4)).strip()
                    if script:
                        col.add(f"${script}$")
            elif cid == _C_GSO:
                inner = self._collect(ctrl.children)
                if inner:
                    col.push(inner)
        col.flush()

    def _table(self, ctrl: _Rec) -> Table | None:
        idx = next((i for i, c in enumerate(ctrl.children) if c.tag == _T_TABLE), None)
        if idx is None:
            return None
        t = ctrl.children[idx].data
        table = Table(rows=_u16(t, 4), cols=_u16(t, 6))
        current: tuple[Cell, list[_Rec]] | None = None

        def finish() -> None:
            if current:
                cell, paras = current
                cell.text = _plain(self._collect(paras))
                table.cells.append(cell)

        for child in ctrl.children[idx + 1 :]:
            if child.tag == _T_LIST_HEADER:
                finish()
                d = child.data
                # 문단 수 필드가 스펙상 INT16 이지만 실제 파일은 대부분 INT32 — 둘 다 시도
                base = 6 if len(d) == 30 else 8
                if _u16(d, base + 2) >= table.rows or _u16(d, base) >= table.cols:
                    base = 6 if base == 8 else 8
                cell = Cell(
                    row=_u16(d, base + 2),
                    col=_u16(d, base),
                    text="",
                    colspan=max(1, _u16(d, base + 4)),
                    rowspan=max(1, _u16(d, base + 6)),
                )
                current = (cell, [])
            elif current:
                current[1].append(child)
        finish()
        return table


# --------------------------------------------------------------------------- #
# HWPX (OWPML: zip + XML)
# --------------------------------------------------------------------------- #


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        el.tag = _local(el.tag)
        if any("}" in k for k in el.attrib):
            el.attrib = {_local(k): v for k, v in el.attrib.items()}
    return root


class _HwpxReader:
    def __init__(self, data: bytes, opts: _Opts):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise HanjiError("broken", "HWPX(zip) 파일을 열 수 없습니다.") from e
        self.opts = opts
        self.styles: dict[str, str] = {}
        self.notes: list[Note] = []

    def _xml(self, name: str) -> ET.Element | None:
        try:
            return _strip_ns(ET.fromstring(self.zip.read(name)))
        except KeyError:
            return None

    def read(self) -> Document:
        names = self.zip.namelist()
        if "mimetype" in names:
            mime = self.zip.read("mimetype").decode(errors="ignore").strip()
            if not mime.startswith("application/hwp"):
                raise HanjiError("unknown-format", f"mimetype 이 {mime!r} 입니다.")

        head = self._xml("Contents/header.xml")
        if head is not None:
            for s in head.iter("style"):
                if "id" in s.attrib:
                    self.styles[s.attrib["id"]] = s.attrib.get("name") or s.attrib.get("engName", "")

        files = sorted(
            (int(m.group(1)), n) for n in names if (m := re.fullmatch(r"Contents/section(\d+)\.xml", n, re.I))
        )
        if not files:
            raise HanjiError("broken", "Contents/sectionN.xml 이 없습니다.")
        sections = [self._collect(self._xml(n)) for _, n in files]

        title = author = None
        hpf = self._xml("Contents/content.hpf")
        if hpf is not None:
            t = hpf.find(".//title")
            title = (t.text or "").strip() or None if t is not None else None
            for m in hpf.iter("meta"):
                if m.attrib.get("name") == "creator":
                    author = (m.text or "").strip() or None

        version = ""
        ver = self._xml("version.xml")
        if ver is not None:
            el = ver if ver.tag == "HCFVersion" else ver.find(".//HCFVersion")
            if el is not None:
                version = f"{el.attrib.get('major', '')}.{el.attrib.get('minor', '')}".strip(".")

        return Document("hwpx", version, title, author, sections, self.notes)

    def _collect(self, container: ET.Element | None) -> list[Block]:
        out: list[Block] = []
        if container is None:
            return out

        def walk(el: ET.Element) -> None:
            for c in el:
                if c.tag == "p":
                    self._paragraph(c, out)
                else:
                    walk(c)

        walk(container)
        return out

    def _paragraph(self, p: ET.Element, out: list[Block]) -> None:
        col = _Collector(out, self.styles.get(p.attrib.get("styleIDRef", "")))

        def visit(el: ET.Element) -> None:
            tag = el.tag
            if tag == "t":
                if el.text:
                    col.add(el.text)
                for c in el:
                    col.add({"tab": "\t", "lineBreak": "\n", "nbSpace": " ", "fwSpace": " ", "hyphen": "-"}.get(c.tag, ""))
                    if c.tail:
                        col.add(c.tail)
            elif tag == "tbl":
                col.push([self._table(el)])
            elif tag in ("footNote", "endNote"):
                if self.opts.notes:
                    note = Note(len(self.notes) + 1, "footnote" if tag == "footNote" else "endnote", "")
                    self.notes.append(note)
                    note.text = _plain(self._collect(el))
                    col.add(f"[^{note.number}]")
            elif tag == "equation":
                script = el.find(".//script")
                if self.opts.equations and script is not None and (script.text or "").strip():
                    col.add(f"${script.text.strip()}$")
            elif tag in ("header", "footer", "secPr", "colPr", "pageNum"):
                return
            elif tag in ("drawText", "subList"):
                inner = self._collect(el)
                if inner:
                    col.push(inner)
            else:
                for c in el:
                    visit(c)

        for c in p:
            visit(c)
        col.flush()

    def _table(self, tbl: ET.Element) -> Table:
        cells = []
        for r, tr in enumerate(x for x in tbl if x.tag == "tr"):
            for c, tc in enumerate(x for x in tr if x.tag == "tc"):
                addr = tc.find("cellAddr")
                span = tc.find("cellSpan")
                cells.append(
                    Cell(
                        row=int(addr.attrib.get("rowAddr", r)) if addr is not None else r,
                        col=int(addr.attrib.get("colAddr", c)) if addr is not None else c,
                        text=_plain(self._collect(tc.find("subList"))),
                        rowspan=max(1, int(span.attrib.get("rowSpan", 1))) if span is not None else 1,
                        colspan=max(1, int(span.attrib.get("colSpan", 1))) if span is not None else 1,
                    )
                )
        rows = int(tbl.attrib.get("rowCnt", 0)) or max((c.row + c.rowspan for c in cells), default=0)
        cols = int(tbl.attrib.get("colCnt", 0)) or max((c.col + c.colspan for c in cells), default=0)
        return Table(rows, cols, cells)


# --------------------------------------------------------------------------- #
# 출력 형식
# --------------------------------------------------------------------------- #


def _one_line(s: str) -> str:
    return re.sub(r"\s*\n\s*", " ", s)


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "<br>")


def _block_md(b: Block) -> str:
    if isinstance(b, Table):
        g = b.grid()
        if not g:
            return ""
        head, *body = g
        lines = ["| " + " | ".join(map(_md_cell, head)) + " |", "|" + " --- |" * len(head)]
        lines += ["| " + " | ".join(map(_md_cell, r)) + " |" for r in body]
        return "\n".join(lines)
    text = b.text.replace("\t", " ")
    return f"{'#' * min(6, b.level)} {_one_line(text)}" if b.level else text


def _block_html(b: Block) -> str:
    if isinstance(b, Paragraph):
        inner = html.escape(b.text).replace("\n", "<br>")
        return f"<h{min(6, b.level)}>{inner}</h{min(6, b.level)}>" if b.level else f"<p>{inner}</p>"
    rows: dict[int, list[Cell]] = {}
    for c in b.cells:
        rows.setdefault(c.row, []).append(c)
    trs = []
    for r in sorted(rows):
        tds = []
        for c in sorted(rows[r], key=lambda c: c.col):
            span = (f' rowspan="{c.rowspan}"' if c.rowspan > 1 else "") + (f' colspan="{c.colspan}"' if c.colspan > 1 else "")
            tds.append(f"<td{span}>{html.escape(c.text).replace(chr(10), '<br>')}</td>")
        trs.append(f"<tr>{''.join(tds)}</tr>")
    return f"<table>{''.join(trs)}</table>"


def _chunk(doc: Document, size: int, overlap: int) -> Iterator[Chunk]:
    trail: list[str] = []
    for si, blocks in enumerate(doc.sections):
        parts: list[str] = []
        start = 0
        heads: list[str] = []
        has_body = False
        carry = ""

        def emit(end: int, keep: bool) -> Iterator[Chunk]:
            nonlocal parts, carry, has_body
            text = "\n\n".join(parts).strip()
            if text:
                yield Chunk(text, si, start, end, heads)
            tail = text[-overlap:] if keep and overlap > 0 else ""
            carry = re.sub(r"^\S*\s+", "", tail)  # 단어 중간에서 시작하지 않도록
            parts, has_body = [], False

        for i, b in enumerate(blocks):
            md = _block_md(b)
            level = b.level if isinstance(b, Paragraph) else None
            if level:
                if has_body:
                    yield from emit(i - 1, False)
                del trail[level - 1 :]
                trail.append(_one_line(b.text))
            elif parts and sum(map(len, parts)) + len(md) > size:
                yield from emit(i - 1, True)
            if not parts:
                start, heads = i, list(trail)
                if carry:
                    parts.append(carry)
                    carry = ""
            if level:
                heads = list(trail)
            else:
                has_body = True
            parts.append(md)
        yield from emit(len(blocks) - 1, False)
