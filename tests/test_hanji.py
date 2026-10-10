"""테스트용 HWPX는 코드로 직접 만듭니다. 실제 .hwp 파일로도 돌려 보려면
HANJI_SAMPLES=/경로/폴더 를 지정하세요 (폴더 안의 *.hwp, *.hwpx 를 모두 읽어 봅니다)."""

import io
import os
import zipfile
from pathlib import Path

import pytest

import hanji

HP = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def p(text, style=0, extra=""):
    return f'<hp:p styleIDRef="{style}"><hp:run><hp:t>{text}</hp:t>{extra}</hp:run></hp:p>'


def cell(r, c, text, rs=1, cs=1):
    return (
        f"<hp:tc><hp:subList>{p(text)}</hp:subList>"
        f'<hp:cellAddr colAddr="{c}" rowAddr="{r}"/><hp:cellSpan colSpan="{cs}" rowSpan="{rs}"/></hp:tc>'
    )


def make_hwpx(body: str, *, title="시험 문서", mimetype="application/hwp+zip") -> bytes:
    header = (
        '<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"><hh:refList><hh:styles>'
        '<hh:style id="0" name="바탕글"/><hh:style id="2" name="개요 1"/><hh:style id="3" name="개요 2"/>'
        "</hh:styles></hh:refList></hh:head>"
    )
    section = f'<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" {HP}>{body}</hs:sec>'
    hpf = (
        '<opf:package xmlns:opf="http://www.idpf.org/2007/opf/"><opf:metadata>'
        f'<opf:title>{title}</opf:title><opf:meta name="creator">홍길동</opf:meta></opf:metadata></opf:package>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        z.writestr("version.xml", '<hv:HCFVersion xmlns:hv="x" major="5" minor="1"/>')
        z.writestr("Contents/header.xml", header)
        z.writestr("Contents/section0.xml", section)
        z.writestr("Contents/content.hpf", hpf)
    return buf.getvalue()


TABLE = (
    '<hp:tbl rowCnt="3" colCnt="3"><hp:tr>'
    + cell(0, 0, "항목")
    + cell(0, 1, "2025", cs=2)
    + "</hp:tr><hp:tr>"
    + cell(1, 0, "매출", rs=2)
    + cell(1, 1, "상반기")
    + cell(1, 2, "A&amp;B | 120")
    + "</hp:tr><hp:tr>"
    + cell(2, 1, "하반기")
    + cell(2, 2, "140")
    + "</hp:tr></hp:tbl>"
)
FOOTNOTE = f'<hp:ctrl><hp:footNote><hp:subList>{p("출처: 통계청")}</hp:subList></hp:footNote></hp:ctrl>'

SAMPLE = (
    f'<hp:p><hp:run><hp:ctrl><hp:header><hp:subList>{p("머리말은 빠져야 함")}</hp:subList></hp:header></hp:ctrl></hp:run></hp:p>'
    + p("1. 사업 개요", 2)
    + p("1.1 추진 배경", 3)
    + f"<hp:p><hp:run><hp:t>매출이 늘었다</hp:t>{FOOTNOTE}<hp:t>.<hp:tab/>탭<hp:lineBreak/>줄바꿈 &lt;b&gt;</hp:t></hp:run></hp:p>"
    + f"<hp:p><hp:run><hp:t>표 앞 문장</hp:t>{TABLE}</hp:run></hp:p>"
    + '<hp:p><hp:run><hp:t>공식 </hp:t><hp:equation><hp:script>E = mc^2</hp:script></hp:equation></hp:run></hp:p>'
    + p("2. 결론", 2)
    + p("글상자 앞", 0, f"<hp:rect><hp:drawText><hp:subList>{p('글상자 안')}</hp:subList></hp:drawText></hp:rect>")
)


@pytest.fixture
def doc():
    return hanji.loads(make_hwpx(SAMPLE))


def test_metadata(doc):
    assert doc.format == "hwpx"
    assert doc.version == "5.1"
    assert (doc.title, doc.author) == ("시험 문서", "홍길동")


def test_headings_from_outline_styles(doc):
    heads = [(b.level, b.text) for b in doc.blocks if isinstance(b, hanji.Paragraph) and b.level]
    assert heads == [(1, "1. 사업 개요"), (2, "1.1 추진 배경"), (1, "2. 결론")]


def test_header_is_skipped(doc):
    assert "머리말" not in doc.to_text()


def test_inline_controls_and_footnote(doc):
    para = next(b for b in doc.blocks if isinstance(b, hanji.Paragraph) and b.text.startswith("매출"))
    assert para.text == "매출이 늘었다[^1].\t탭\n줄바꿈 <b>"
    assert doc.notes == [hanji.Note(1, "footnote", "출처: 통계청")]


def test_table_spans_and_reading_order(doc):
    texts = [b.text if isinstance(b, hanji.Paragraph) else "<table>" for b in doc.blocks]
    assert texts[texts.index("<table>") - 1] == "표 앞 문장"
    (table,) = doc.tables
    assert table.grid() == [["항목", "2025", ""], ["매출", "상반기", "A&B | 120"], ["", "하반기", "140"]]
    spans = {(c.row, c.col): (c.rowspan, c.colspan) for c in table.cells}
    assert spans[(0, 1)] == (1, 2) and spans[(1, 0)] == (2, 1)


def test_markdown(doc):
    md = doc.to_markdown()
    assert md.startswith("# 1. 사업 개요\n\n## 1.1 추진 배경")
    assert "| 매출 | 상반기 | A&B \\| 120 |" in md
    assert "$E = mc^2$" in md
    assert md.endswith("[^1]: 출처: 통계청")


def test_html_escapes_and_spans(doc):
    h = doc.to_html()
    assert '<td colspan="2">2025</td>' in h and '<td rowspan="2">매출</td>' in h
    assert "줄바꿈 &lt;b&gt;" in h and "A&amp;B" in h


def test_text_box_kept_in_order(doc):
    texts = [b.text for b in doc.blocks if isinstance(b, hanji.Paragraph)]
    assert texts[-2:] == ["글상자 앞", "글상자 안"]


def test_chunks_carry_heading_trail(doc):
    chunks = doc.chunks(size=60, overlap=0)
    assert all(c.text for c in chunks)
    first_body = next(c for c in chunks if "매출이" in c.text)
    assert first_body.headings == ["1. 사업 개요", "1.1 추진 배경"]
    assert chunks[-1].headings == ["2. 결론"]
    assert chunks[0].id.startswith("s0b0")


def test_options_turn_things_off():
    d = hanji.loads(make_hwpx(SAMPLE), notes=False, equations=False)
    assert d.notes == [] and "[^1]" not in d.to_text() and "mc^2" not in d.to_text()


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"hello world", "unknown-format"),
        (b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05", "hwp3"),
        (b"PK\x03\x04 not really a zip", "broken"),
        (b"\xd0\xcf\x11\xe0 not really ole", "broken"),
    ],
)
def test_errors(data, reason):
    with pytest.raises(hanji.HanjiError) as e:
        hanji.loads(data)
    assert e.value.reason == reason


def test_rejects_other_zip_formats():
    with pytest.raises(hanji.HanjiError) as e:
        hanji.loads(make_hwpx(p("x"), mimetype="application/epub+zip"))
    assert e.value.reason == "unknown-format"


def test_cli(tmp_path, capsys):
    from hanji.__main__ import main

    f = tmp_path / "a.hwpx"
    f.write_bytes(make_hwpx(SAMPLE))
    assert main([str(f), "--text"]) == 0
    assert "글상자 안" in capsys.readouterr().out
    assert main([str(tmp_path / "없는파일.hwp")]) == 1


SAMPLES = os.environ.get("HANJI_SAMPLES")


@pytest.mark.skipif(not SAMPLES, reason="HANJI_SAMPLES 미지정")
@pytest.mark.parametrize("path", sorted(Path(SAMPLES or ".").glob("*.hwp*")), ids=lambda p: p.name)
def test_real_files(path):
    d = hanji.load(path)
    assert d.to_text().strip()
    assert d.to_markdown()
