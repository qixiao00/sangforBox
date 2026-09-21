"""探测三份 AF 8.0.107 PDF 的结构：页数、文本层、目录、字号分布。

只做只读探测，用于决定解析策略，不产出知识库数据。
"""

from __future__ import annotations

import collections
import hashlib
import json
import sys
from pathlib import Path

import pymupdf

DOCS = [
    ("af-8.0.107-api", "D:/chrome_downloads/AF8.0.107版本API帮忙文档.pdf"),
    ("af-8.0.107-cli", "D:/chrome_downloads/AF8.0.107版本CLI帮忙文档.pdf"),
    ("af-8.0.107-product", "D:/chrome_downloads/下一代防火墙AF_AF8.0.107_文档.pdf"),
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    report = []
    for doc_id, raw in DOCS:
        path = Path(raw)
        if not path.exists():
            print(f"MISSING {doc_id} {raw}")
            continue
        doc = pymupdf.open(path)
        toc = doc.get_toc(simple=True)
        char_counts = []
        empty_pages = []
        size_hist = collections.Counter()
        font_hist = collections.Counter()
        image_pages = 0
        for i, page in enumerate(doc):
            text = page.get_text("text")
            char_counts.append(len(text.strip()))
            if len(text.strip()) < 20:
                empty_pages.append(i + 1)
            if page.get_images(full=True):
                image_pages += 1
            d = page.get_text("dict")
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span["text"].strip():
                            size_hist[round(span["size"], 1)] += len(span["text"].strip())
                            font_hist[span["font"]] += len(span["text"].strip())
        print(f"\n===== {doc_id} =====")
        print(f"path        : {raw}")
        print(f"sha256      : {sha256_of(path)}")
        print(f"file_size   : {path.stat().st_size}")
        print(f"pages       : {doc.page_count}")
        print(f"toc_entries : {len(toc)}")
        print(f"total_chars : {sum(char_counts)}")
        print(f"pages<20chr : {len(empty_pages)} -> {empty_pages[:40]}")
        print(f"pages w/img : {image_pages}")
        print(f"top sizes   : {size_hist.most_common(12)}")
        print(f"top fonts   : {font_hist.most_common(8)}")
        print("toc head    :")
        for lvl, title, pg in toc[:30]:
            print(f"   {lvl} | p{pg} | {title}")
        report.append(
            {
                "doc_id": doc_id,
                "path": raw,
                "pages": doc.page_count,
                "toc_entries": len(toc),
                "total_chars": sum(char_counts),
                "empty_pages": empty_pages,
                "image_pages": image_pages,
                "size_hist": size_hist.most_common(20),
                "font_hist": font_hist.most_common(10),
                "toc": [{"level": l, "title": t, "page": p} for l, t, p in toc],
            }
        )
        doc.close()
    out = Path("C:/Users/18066/Documents/ChatGPT/sangforBox/knowledge/scripts/_probe.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
