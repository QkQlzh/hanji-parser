"""python -m hanji 문서.hwp [--md | --html | --json | --chunks]"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import HanjiError, __version__, load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hanji", description="HWP/HWPX 문서를 텍스트로 꺼냅니다.")
    ap.add_argument("files", nargs="+", type=Path, help=".hwp 또는 .hwpx 파일")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--md", action="store_const", dest="fmt", const="md", help="마크다운 (기본값)")
    fmt.add_argument("--text", action="store_const", dest="fmt", const="text", help="일반 텍스트")
    fmt.add_argument("--html", action="store_const", dest="fmt", const="html", help="HTML 조각")
    fmt.add_argument("--json", action="store_const", dest="fmt", const="json", help="블록 구조 그대로")
    fmt.add_argument("--chunks", action="store_const", dest="fmt", const="chunks", help="RAG 청크 (JSON Lines)")
    ap.add_argument("--size", type=int, default=800, help="--chunks 청크 크기 (글자 수, 기본 800)")
    ap.add_argument("-o", "--out", type=Path, help="파일 하나일 때 저장할 경로")
    ap.add_argument("-V", "--version", action="version", version=f"hanji {__version__}")
    args = ap.parse_args(argv)
    fmt = args.fmt or "md"

    failed = 0
    for path in args.files:
        try:
            doc = load(path)
        except (HanjiError, OSError) as e:
            print(f"hanji: {path}: {e}", file=sys.stderr)
            failed += 1
            continue

        if fmt == "md":
            out = doc.to_markdown()
        elif fmt == "text":
            out = doc.to_text()
        elif fmt == "html":
            out = doc.to_html()
        elif fmt == "json":
            out = json.dumps(asdict(doc), ensure_ascii=False, indent=2)
        else:
            out = "\n".join(
                json.dumps({"id": c.id, "file": path.name, **asdict(c)}, ensure_ascii=False)
                for c in doc.chunks(size=args.size)
            )

        if args.out and len(args.files) == 1:
            args.out.write_text(out + "\n", encoding="utf-8")
        else:
            if len(args.files) > 1 and fmt in ("md", "text"):
                print(f"\n===== {path.name} =====\n")
            print(out)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
