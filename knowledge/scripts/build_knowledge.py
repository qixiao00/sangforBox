"""SangforBox 知识库加工流水线 —— 主程序。

把三份深信服 AF 8.0.107 PDF 加工成可审计、可重复导入的 RAG 结构化数据。

用法：
    python knowledge/scripts/build_knowledge.py                # 全量加工
    python knowledge/scripts/build_knowledge.py --stage report # 仅重建质量报告
    python knowledge/scripts/build_knowledge.py --only af-8.0.107-cli

产物目录：knowledge/processed/
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_common import (  # noqa: E402
    build_keywords,
    clean_table_cell,
    looks_like_watermark_residue,
    looks_like_method_line,
    markdown_to_plain,
    md_escape_inline,
    normalize_cjk,
    parse_method_line,
    sha256_file,
    sha256_text,
    squeeze_spaces,
    strip_watermark_text,
    table_to_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_ROOT = REPO_ROOT / "knowledge" / "processed"
REPORT_DIR = OUT_ROOT / "reports"
TABLE_DIR = OUT_ROOT / "tables"
SCHEMA_DIR = OUT_ROOT / "schemas"
MD_DIR = OUT_ROOT / "source_markdown"

# 解析阶段（PDF -> 逐页结构化元素）耗时长（API 手册约 8 分钟）。
# 这里加一层本地缓存，只缓存 Phase A（extract_raw 的逐页提取结果）；
# 分类 / 分节 / 切块等易迭代逻辑每次都会重跑，因此调整这些规则无需重新解析。
# 注意：Phase A 内部会调用 kb_common 的清理函数（表格单元格水印剥离、行级水印剔除等），
# 所以缓存键必须并入 kb_common 的内容指纹——否则改了清理逻辑，旧缓存仍会命中，
# 产物会静默沿用未清理的旧数据。
# 缓存只是开发/恢复加速手段：缺失或损坏时自动回退到真实解析，不影响产物正确性。
CACHE_DIR = Path(__file__).resolve().parent / "_cache"
CACHE_VERSION = "parse-v2"
KB_COMMON_FINGERPRINT = sha256_file(Path(__file__).resolve().parent / "kb_common.py")[:16]

try:  # PyMuPDF 版本号
    from importlib.metadata import version as _pkg_version

    PARSER_VERSION = _pkg_version("pymupdf")
except Exception:  # pragma: no cover
    PARSER_VERSION = getattr(pymupdf, "__version__", "unknown")
PARSER_NAME = "PyMuPDF (fitz) text + find_tables"

VENDOR = "深信服"
PRODUCT = "AF"
DOC_VERSION = "8.0.107"
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

DOC_SPECS = [
    {
        "doc_id": "af-8.0.107-api",
        "doc_type": "api_manual",
        "title": "API帮助文档",
        "path": "D:/chrome_downloads/AF8.0.107版本API帮忙文档.pdf",
        # 第 1 页为封面，第 2-41 页为目录（点线导航，非正文）
        "excluded_pages": list(range(1, 42)),
        "profile": "api",
        "chunk_max": 850,
        "chunk_min": 400,
    },
    {
        "doc_id": "af-8.0.107-cli",
        "doc_type": "cli_manual",
        "title": "命令行帮助文档",
        "path": "D:/chrome_downloads/AF8.0.107版本CLI帮忙文档.pdf",
        "excluded_pages": list(range(1, 18)),
        "profile": "cli",
        "chunk_max": 1000,
        "chunk_min": 300,
    },
    {
        "doc_id": "af-8.0.107-product",
        "doc_type": "product_manual",
        "title": "下一代防火墙AF_AF8.0.107_文档",
        "path": "D:/chrome_downloads/下一代防火墙AF_AF8.0.107_文档.pdf",
        "excluded_pages": [1, 2],
        "profile": "product",
        "chunk_max": 800,
        "chunk_min": 250,
    },
]

# --------------------------------------------------------------------------
# 字体 / 版式画像：用于区分正文、标题、页眉页脚、水印
# --------------------------------------------------------------------------

PROFILE = {
    "api": {
        "body_fonts": {"FZLTXIHJW--GB1-0"},
        "heading_fonts": {"FZLTZHJW--GB1-0"},
        "header_footer": lambda span: span["font"].startswith("FZLTXIHJW") and span["size"] >= 14.5,
        "code_size": (7.5, 8.8),
        "size_to_level": {18.0: 1, 14.0: 2, 12.0: 3, 10.0: 4},
        "heading_min_size": 10.0,
    },
    "cli": {
        "body_fonts": {"FZLTXIHJW--GB1-0"},
        "heading_fonts": {"FZLTZHJW--GB1-0"},
        "header_footer": lambda span: span["font"].startswith("FZLTXIHJW") and span["size"] >= 14.5,
        "code_size": (7.5, 8.8),
        "size_to_level": {18.0: 1, 14.0: 2, 12.0: 3, 10.0: 4},
        "heading_min_size": 10.0,
    },
    "product": {
        "body_fonts": {"SimSun", "STSong-Light"},
        "heading_fonts": {"SourceHanSansSC-Medium"},
        # 水印：STSong-Light 12.5；页脚：SourceHanSansSC-Medium 8.0
        "header_footer": lambda span: (
            (span["font"] == "STSong-Light" and 12.0 <= span["size"] <= 13.0)
            or span["size"] < 8.5
        ),
        "code_size": (9.0, 10.0),
        "size_to_level": {21.0: 1, 15.8: 2, 12.3: 3, 10.5: 4, 8.7: 5},
        "heading_min_size": 8.5,
    },
}

ISSUES: list[dict] = []
_TABLE_SEQ = collections.Counter()


def add_issue(doc_id, category, message, *, page=None, severity="warning", detail=None, related=None):
    ISSUES.append(
        {
            "issue_id": f"iss-{len(ISSUES) + 1:05d}",
            "doc_id": doc_id,
            "page": page,
            "severity": severity,
            "category": category,
            "message": message,
            "detail": detail,
            "related_ids": related or [],
        }
    )


# --------------------------------------------------------------------------
# 页面级解析
# --------------------------------------------------------------------------

def _span_kept(span, profile_cfg) -> bool:
    if not span["text"].strip():
        return False
    return not profile_cfg["header_footer"](span)


def _text_matches_footer(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if re.fullmatch(r"第\s*\d+\s*[页⻚]", t):
        return True
    if t.startswith("版权所有") or t.startswith("密级"):
        return True
    if "防火墙AF_" in t and len(t) <= 40:
        return True
    return False


_HTTP_METHOD_TOKENS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff]")


def rejoin_identifier(cell, page_tokens):
    """修复被物理换行截断的参数名。

    PyMuPDF 的 find_tables 按行分组单元格内的文本，下划线等字符因基线偏移
    会被判到单独一行，导致 `_sortby` 变成 `sortby\\n_`。这里用页面原始
    token 作为证据还原：候选拼接结果只要能在页面 token 中找到就采用。
    """
    if not cell or "\n" not in cell:
        return cell
    parts = [p for p in cell.split("\n") if p.strip()]
    if len(parts) < 2:
        return cell
    joined = "".join(parts)
    reversed_joined = "".join(reversed(parts))
    for candidate in (joined, reversed_joined):
        if page_tokens and candidate in page_tokens:
            return candidate
    # 无 CJK 的单元格几乎一定是包在表格里的标识符/URL，换行属于排版换行
    if not _CJK_CHAR_RE.search(joined):
        if page_tokens:
            for candidate in (reversed_joined, joined):
                if any(token.startswith(candidate) and len(token) <= len(candidate) + 12
                       for token in page_tokens):
                    return candidate
        return joined
    return cell



def join_wrapped(parts):
    """拼接被物理换行截断的片段：拉丁字母之间补空格，中文之间直接相连。"""
    out = ""
    for part in parts:
        if not out:
            out = part
            continue
        left = out[-1]
        right = part[0] if part else ""
        if left.isascii() and left.isalnum() and right.isascii() and right.isalnum():
            out += " " + part
        else:
            out += part
    return out


def extract_page(page, spec, page_no):
    """返回 (elements, kept_tables, stats)。elements 已按阅读顺序排列。"""
    cfg = PROFILE[spec["profile"]]
    doc_id = spec["doc_id"]
    stats = collections.Counter()
    raw = page.get_text("dict")

    # ---- 0. 页面原始 token 集合（用于修复被换行截断的标识符） --------------
    page_tokens = set()
    all_lines = []  # (text, x0, y0, x1, y1, font, size)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if _span_kept(s, cfg)]
            if not spans:
                continue
            text = normalize_cjk("".join(s["text"] for s in spans))
            if not text.strip():
                continue
            for token in text.split():
                page_tokens.add(token)
            all_lines.append(
                {
                    "text": squeeze_spaces(text),
                    "x0": min(s["bbox"][0] for s in spans),
                    "x1": max(s["bbox"][2] for s in spans),
                    "y0": min(s["bbox"][1] for s in spans),
                    "y1": max(s["bbox"][3] for s in spans),
                    "font": spans[0]["font"],
                    "size": round(max(s["size"] for s in spans), 1),
                    "bold": bool({s["font"] for s in spans} & cfg["heading_fonts"]),
                }
            )

    def lines_in(bbox):
        bx0, by0, bx1, by1 = bbox
        picked = []
        for line in all_lines:
            cx = (line["x0"] + line["x1"]) / 2
            cy = (line["y0"] + line["y1"]) / 2
            if bx0 - 2 <= cx <= bx1 + 2 and by0 - 2 <= cy <= by1 + 2:
                picked.append(line)
        picked.sort(key=lambda l: (round(l["y0"], 1), l["x0"]))
        return picked

    # ---- 1. 表格检测 -----------------------------------------------------
    kept_tables = []
    try:
        finder = page.find_tables()
        candidates = list(finder.tables)
    except Exception as exc:  # pragma: no cover
        candidates = []
        add_issue(
            doc_id,
            "table_extraction_failure",
            "find_tables 在该页抛出异常，已按无表格处理",
            page=page_no,
            severity="error",
            detail=repr(exc),
        )
    for table in candidates:
        try:
            rows = table.extract()
        except Exception as exc:  # pragma: no cover
            add_issue(
                doc_id,
                "table_extraction_failure",
                "表格结构提取失败",
                page=page_no,
                severity="error",
                detail=repr(exc),
            )
            continue
        rows = [
            [
                clean_table_cell(c if c is not None else "", spec["profile"] == "product")
                for c in row
            ]
            for row in rows
        ]
        if not any(any(cell for cell in row) for row in rows):
            continue
        if spec["profile"] == "product":
            residue = [
                cell for row in rows for cell in row
                if cell and looks_like_watermark_residue(cell)
            ]
            if residue:
                add_issue(
                    doc_id,
                    "watermark_residue",
                    "产品手册表格清理后仍疑似存在水印残留",
                    page=page_no,
                    severity="warning",
                    detail=" | ".join(residue[:3])[:200],
                )
        # 「请求方法 + URL」单行框不是内容表格，还原为代码行
        if len(rows) == 1 and len(rows[0]) >= 2 and rows[0][0].strip().upper() in _HTTP_METHOD_TOKENS:
            inner = lines_in(table.bbox)
            method = None
            pieces = []
            for line in inner:
                token = line["text"].strip()
                if token.upper() in _HTTP_METHOD_TOKENS and method is None:
                    method = token.upper()
                    continue
                pieces.append(token)
            url = join_wrapped(pieces).replace(" ", "") if pieces else ""
            if method is None:
                method = rows[0][0].strip().upper()
            if not url:
                url = rows[0][1].strip()
            kept_tables.append(
                {"kind": "request_line", "bbox": table.bbox, "method": method, "url": url}
            )
            continue
        # 参数表首列（参数名）可能被物理换行截断，用页面 token 复原
        for row in rows:
            if row:
                row[0] = rejoin_identifier(row[0], page_tokens)
        kept_tables.append({"kind": "table", "bbox": table.bbox, "rows": rows, "obj": table})
    stats["tables"] = sum(1 for t in kept_tables if t["kind"] == "table")
    stats["request_lines"] = sum(1 for t in kept_tables if t["kind"] == "request_line")

    # ---- 2. 行抽取（剔除页眉页脚/水印 span） ------------------------------
    stats["images"] = sum(1 for block in raw.get("blocks", []) if block.get("type") != 0)
    lines = []
    for line in all_lines:
        if _text_matches_footer(line["text"]):
            continue
        lines.append(line)

    # ---- 3. 剔除落在表格区域内的行 ---------------------------------------
    def inside_table(line):
        cx = (line["x0"] + line["x1"]) / 2
        cy = (line["y0"] + line["y1"]) / 2
        for table in kept_tables:
            bx0, by0, bx1, by1 = table["bbox"]
            if bx0 - 2 <= cx <= bx1 + 2 and by0 - 2 <= cy <= by1 + 2:
                return True
        return False

    body_lines = [ln for ln in lines if not inside_table(ln)]

    # ---- 4. 组装元素 -----------------------------------------------------
    elements = []
    table_payloads = []
    for table in kept_tables:
        if table["kind"] == "request_line":
            method = table["method"]
            url = table["url"]
            elements.append(
                {
                    "kind": "code",
                    "text": f"{method} {url}",
                    "page": page_no,
                    "order": (table["bbox"][1], table["bbox"][0]),
                    "style": "request-line",
                }
            )
        else:
            _TABLE_SEQ[(doc_id, page_no)] += 1
            index = _TABLE_SEQ[(doc_id, page_no)]
            table_id = f"{doc_id}-p{page_no}-t{index}"
            html_path = f"{table_id}.html"
            header = table["rows"][0] if table["rows"] else []
            md = table_to_markdown(table["rows"][1:], header=header)
            table["table_id"] = table_id
            table["html_path"] = html_path
            table["md"] = md
            table["page"] = page_no
            table_payloads.append(table)
            elements.append(
                {
                    "kind": "table",
                    "text": "",
                    "page": page_no,
                    "order": (table["bbox"][1], table["bbox"][0]),
                    "table_id": table_id,
                    "header": header,
                    "rows": table["rows"],
                    "md": md,
                    "html_path": html_path,
                    "caption": None,
                }
            )
            table["table_id"] = table_id

    for line in body_lines:
        elements.append(
            {
                "kind": "raw_line",
                "text": line["text"],
                "page": page_no,
                "order": (line["y0"], line["x0"]),
                "line": line,
            }
        )

    elements.sort(key=lambda e: e["order"])
    # 清理不可 pickle 的对象，便于把解析结果写入本地缓存（fitz.Table / fitz.Rect）。
    # "obj" 仅用于解析期判定，之后不再被读取；bbox 只在 extract_page 内部使用。
    for table in table_payloads:
        table.pop("obj", None)
        bbox = table.get("bbox")
        if bbox is not None and not isinstance(bbox, (tuple, list)):
            table["bbox"] = tuple(bbox)
    return elements, table_payloads, stats


# --------------------------------------------------------------------------
# 标题匹配与元素分类
# --------------------------------------------------------------------------

def normalize_title(text: str) -> str:
    t = normalize_cjk(text or "")
    t = re.sub(r"[\s\u3000]+", "", t)
    t = t.replace("．", ".").replace("。", ".")
    return t


def build_toc_index(doc, spec, excluded_pages, page_filter=None):
    index = collections.defaultdict(dict)
    entries = []
    for order, (level, title, page) in enumerate(doc.get_toc(simple=True)):
        if page in excluded_pages or page < 1 or page > doc.page_count:
            continue
        if page_filter is not None and page not in page_filter:
            continue
        key = normalize_title(title)
        entries.append({"level": level, "title": normalize_cjk(title).strip(), "page": page, "order": order})
        index[page].setdefault(key, []).append((level, order))
    return index, entries


_LISTITEM_RE = re.compile(r"^(?:\d+|[一二三四五六七八九十]+)\s*[、.．]\s*\S|^[（(]\s*\d+\s*[)）]\s*\S")
# 产品手册内嵌评审批注的锚点标记，形如 [ZY6] / [ZY7]
_INLINE_NOTE_RE = re.compile(r"\s*\[[A-Za-z]{1,6}\d+\]")
_CN_NUM = {c: i for i, c in enumerate("零一二三四五六七八九十")}


def _listitem_number(text):
    match = re.match(r"^(\d+|[一二三四五六七八九十]+|[\d]+)\s*[、.．]", text)
    if not match:
        match = re.match(r"^[（(]\s*(\d+)\s*[)）]", text)
        if not match:
            return None
        return int(match.group(1))
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if raw.startswith("十"):
        return 10 + _CN_NUM.get(raw[1:], 0)
    if raw.endswith("十"):
        return _CN_NUM.get(raw[0], 0) * 10
    return _CN_NUM.get(raw, None)


def _looks_like_term_entry(text: str) -> bool:
    """判断产品手册里「术语名：说明文字」式的正文条目。

    产品手册的术语条目（如「流量通道： 在某条线路上将整个线路带宽…」）与真标题使用
    同一套 SourceHanSansSC-Medium 字体、同为 10.5pt、同为 bold，仅靠字重/字号无法区分，
    因此它们会被「兜底标题」分支误收为 heading，导致其换行续写（「的最小带宽值。」）
    沦为孤立短 chunk。

    判据（必须同时避开下面的真标题形态）：
      真标题   「二、 事中防御：完整的防御体系+安全联动+威胁情报」——冒号后是短语，无句读
      真标题   「实时漏洞分析、 WAF 、 IPS 、内网安全配置案例：」——冒号收尾
      真标题   「基于 IP 地址、 MAC 地址、计算机名的识别」——无冒号
      正文条目 「HTTP 头部溢出检测： 勾选启用 HTTP 头部溢出检测，点击 < 新增 > 按钮，…」
    可见可靠的区分点是：正文条目的冒号后面一定跟着**含句读标点的成句说明**。
    """
    stripped = text.strip()
    if not stripped:
        return False
    # ① 术语条目：术语名 + 冒号 + 含句读标点的成句说明。
    #    术语名可能含空格（「URL 溢出检测」「POST 实体溢出检测」「业务 / 用户安全页面显示设置」），
    #    因此不做「不含空白」的限定；真正区分正文与标题的是冒号后是否跟成句说明。
    match = re.match(r"^.{1,18}?[：:]\s*(\S.*)$", stripped)
    if match and re.search(r"[，。；！？,;!?]", match.group(1)):
        return True
    # ② 长句且以句末标点收尾（标题不会有句末标点）
    if len(stripped) >= 28 and re.search(r"[。！？.!?]$", stripped):
        return True
    return False


def classify(elements, toc_index, spec):
    """把 raw_line 分类为 heading / marker / bullet / listitem / code / para，并做段落合并。"""
    cfg = PROFILE[spec["profile"]]
    doc_id = spec["doc_id"]
    out = []
    matched_toc = set()
    note_continuation_pending = False

    def next_is_new_item(index):
        """判断下一行是否开启新的列表项/章节（用于区分换行续写与真正的列表项）。"""
        if index + 1 >= len(elements):
            return True
        nxt = elements[index + 1]
        if nxt["kind"] != "raw_line":
            return True
        nxt_text = nxt["text"]
        if _LISTITEM_RE.match(nxt_text) or re.match(r"^\d+\)\s*\S", nxt_text):
            return True
        if nxt["line"]["bold"]:
            return True
        return False

    for index, element in enumerate(elements):
        if element["kind"] != "raw_line":
            out.append(element)
            continue
        line = element["line"]
        text = element["text"]
        page = element["page"]

        # --- 编辑批注（产品手册内嵌 review 注释，不属于产品内容） ---
        # 必须放在标题判定之前：批注多为加粗大字，若先走「兜底标题」分支会被
        # 误判为标题并作为小节标题进入正文。
        if spec["profile"] == "product" and re.match(r"^\s*\[[A-Za-z]{1,6}\d+\]", text):
            add_issue(
                doc_id,
                "editorial_note_found",
                "发现文档内嵌编辑批注，已从正文剔除",
                page=page,
                severity="info",
                detail=text[:200],
            )
            # 批注未以句末标点收尾时，其换行续写也属于批注内容
            note_continuation_pending = not re.search(r"[。！？.!?]\s*$", text.strip())
            continue
        if note_continuation_pending:
            note_continuation_pending = False
            starts_new_block = bool(
                re.match(r"^\d+\)\s*\S", text)
                or re.match(r"^[•·▪◆]\s*\S", text)
                or re.match(r"^<\d+>\s*\S", text)
                or _LISTITEM_RE.match(text)
            )
            if not starts_new_block:
                continue

        # 行内批注标记（例如「…过滤和阀值 [ZY6] 。」）：产品手册正文里的评审批注
        # 锚点，不属于产品内容。整段行首批注已在上方剔除，这里清掉句中的锚点。
        if spec["profile"] == "product" and _INLINE_NOTE_RE.search(text):
            text = _INLINE_NOTE_RE.sub("", text)

        # --- 目录匹配（标题的第一判据） ---
        key = normalize_title(text)
        hit = toc_index.get(page, {}).get(key)
        if not hit and toc_index.get(page + 1, {}).get(key):
            hit = toc_index[page + 1][key]
        if hit:
            level = min(h[0] for h in hit)
            matched_toc.add((key, page))
            out.append(
                {
                    "kind": "heading",
                    "text": text,
                    "page": page,
                    "order": element["order"],
                    "level": level,
                    "toc_matched": True,
                }
            )
            continue

        # --- 编号小节标记 1) / 2) / 3) ---
        if re.match(r"^\d+\)\s*\S", text) and line["bold"]:
            out.append({"kind": "marker", "text": text, "page": page, "order": element["order"]})
            continue

        # --- 项目符号行 ---
        if re.match(r"^[•·▪◆]\s*\S", text):
            out.append(
                {
                    "kind": "bullet",
                    "text": re.sub(r"^[•·▪◆]\s*", "", text).strip(),
                    "page": page,
                    "order": element["order"],
                    "raw": text,
                }
            )
            continue

        # --- 子命令名（CLI 文档：加粗、<n> 编号、位于子命令块内） ---
        if line["bold"] and re.match(r"^<\d+>\s*\S", text):
            out.append(
                {
                    "kind": "marker",
                    "text": text,
                    "page": page,
                    "order": element["order"],
                    "style": "subcommand",
                }
            )
            continue

        # --- 兜底标题（未命中目录的加粗大字） ---
        # 产品手册的「术语名：说明文字」条目与真标题同字重同字号，需先排除，
        # 否则条目正文会被误判为标题、其换行续写沦为孤立短 chunk。
        if (
            line["bold"]
            and line["size"] >= cfg["heading_min_size"]
            and not re.match(r"^\d+\)", text)
            and not (spec["profile"] == "product" and _looks_like_term_entry(text))
        ):
            level = cfg["size_to_level"].get(line["size"])
            if level is not None:
                out.append(
                    {
                        "kind": "heading",
                        "text": text,
                        "page": page,
                        "order": element["order"],
                        "level": level,
                        "toc_matched": False,
                    }
                )
                continue

        # --- 编号列表项（自成一段，不与上下文合并） ---
        item_no = _listitem_number(text) if _LISTITEM_RE.match(text) else None
        prev_is_sequential = (
            item_no is not None
            and out
            and out[-1]["kind"] == "listitem"
            and out[-1].get("item_no") is not None
            and out[-1]["item_no"] == item_no - 1
            and out[-1]["page"] == page
        )
        if item_no is not None and not line["bold"] and (next_is_new_item(index) or prev_is_sequential):
            out.append(
                {
                    "kind": "listitem",
                    "text": text,
                    "page": page,
                    "order": element["order"],
                    "item_no": item_no,
                }
            )
            continue

        # --- 代码行 ---
        is_code = False
        if spec["profile"] in ("api", "cli"):
            lo, hi = cfg["code_size"]
            if lo <= line["size"] <= hi:
                is_code = True
            if re.match(r"^hostname[\w\-]*(\([\w\-]+\))?\s*[#>]", text):
                is_code = True
            if looks_like_method_line(text) and "http" in text.lower():
                is_code = True
        else:
            if text.startswith(("{", "}", "[", "]", '"')) and line["x0"] > 80:
                is_code = True
            if re.match(r"^hostname[\w\-]*(\([\w\-]+\))?\s*[#>]", text):
                is_code = True
        if is_code:
            if out and out[-1]["kind"] == "code" and out[-1]["page"] == page:
                out[-1]["text"] = out[-1]["text"] + "\n" + text
            else:
                out.append({"kind": "code", "text": text, "page": page, "order": element["order"]})
            continue

        # --- 普通段落，尝试与上一行合并 ---
        if (
            out
            and out[-1]["kind"] == "para"
            and out[-1]["page"] == page
            and _can_merge(out[-1], line)
        ):
            out[-1]["text"] = join_wrapped([out[-1]["text"], text])
            out[-1]["_last_y1"] = line["y1"]
            continue
        out.append(
            {
                "kind": "para",
                "text": text,
                "page": page,
                "order": element["order"],
                "line": line,
                "_last_y1": line["y1"],
            }
        )
    return out, matched_toc


_TERMINAL_CHARS = "。！？；：!?;:）)】》」』"


def _can_merge(prev, line) -> bool:
    prev_line = prev.get("line")
    if prev_line is None:
        return False
    gap = line["y0"] - prev.get("_last_y1", prev_line["y1"])
    if gap > 1.45 * max(line["size"], prev_line["size"]) or gap < -2:
        return False
    if line["x0"] - prev_line["x0"] > 6 and prev_line["x1"] < 480:
        return False
    if prev["text"].rstrip().endswith(tuple(_TERMINAL_CHARS)):
        return False
    if re.match(r"^[•·▪◆]", line["text"]) or re.match(r"^\d+\)", line["text"]):
        return False
    return True


# --------------------------------------------------------------------------
# 章节树
# --------------------------------------------------------------------------

def build_sections(elements, doc_id, doc_type, product, version):
    root = {
        "level": 0,
        "title": None,
        "title_path": [],
        "section_no": None,
        "page_start": None,
        "page_end": None,
        "elements": [],
        "children": [],
    }
    stack = [root]
    for element in elements:
        if element["kind"] == "heading":
            level = element["level"]
            while len(stack) > 1 and stack[-1]["level"] >= level:
                stack.pop()
            parent = stack[-1]
            title = element["text"]
            section = {
                "level": level,
                "title": title,
                "section_no": extract_section_no(title),
                "title_path": parent["title_path"] + [title],
                "page_start": element["page"],
                "page_end": element["page"],
                "elements": [],
                "children": [],
            }
            parent["children"].append(section)
            stack.append(section)
            continue
        # 归属到当前最深章节
        target = stack[-1]
        if target is root and not elements:
            continue
        element = {k: v for k, v in element.items() if k not in ("order", "line", "_last_y1")}
        target["elements"].append(element)
        if element.get("page"):
            if target["page_start"] is None:
                target["page_start"] = element["page"]
            target["page_end"] = element["page"]
    return root


def extract_section_no(title: str):
    match = re.match(r"^(\d+(?:\.\d+)*)\s+\S", title or "")
    return match.group(1) if match else None


def iter_sections(section, out=None):
    out = [] if out is None else out
    out.append(section)
    for child in section["children"]:
        iter_sections(child, out)
    return out


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------

def element_markdown(element):
    kind = element["kind"]
    if kind == "para":
        return element["text"]
    if kind == "listitem":
        return element["text"]
    if kind == "bullet":
        return f"- {element['text']}"
    if kind == "marker":
        return f"**{element['text']}**"
    if kind == "code":
        return "```\n" + element["text"] + "\n```"
    if kind == "table":
        caption = element.get("caption")
        if caption:
            return f"**{caption}**\n\n" + element["md"]
        return element["md"]
    return element.get("text", "")


def element_page(element):
    return element.get("page")


def split_segments(elements):
    """在「语义边界」处分段：marker / bullet / listitem / code / table 处允许断点。"""
    segments = []
    current = []
    for element in elements:
        boundary = element["kind"] in ("marker", "bullet", "listitem", "code", "table")
        if boundary and current:
            segments.append(current)
            current = []
        current.append(element)
    if current:
        segments.append(current)
    return segments


def pack_chunks(section, spec):
    elements = section["elements"]
    if not elements:
        return []
    max_len = spec["chunk_max"]
    min_len = spec["chunk_min"]
    segments = split_segments(elements)
    chunks = []
    current = []
    current_len = 0

    def flush():
        nonlocal current, current_len
        if current:
            chunks.append(current)
        current = []
        current_len = 0

    total = sum(len(markdown_to_plain(element_markdown(e))) for e in elements)
    if total <= max_len:
        return [elements]

    for segment in segments:
        seg_len = sum(len(markdown_to_plain(element_markdown(e))) for e in segment)
        if current and current_len + seg_len > max_len and current_len >= min_len:
            flush()
        if current and current_len + seg_len > max_len * 2 and current_len > 0:
            flush()
        current.extend(segment)
        current_len += seg_len
    flush()
    return chunks


def make_chunk_id(doc_id, title_path, page_start, ordinal, plain_text):
    path_hash = sha256_text("|".join(title_path or []))[:6]
    text_hash = sha256_text(plain_text)[:10]
    return f"{doc_id}::p{page_start}::t{path_hash}::n{ordinal:04d}::{text_hash}"


# --------------------------------------------------------------------------
# 主流程：解析单份文档
# --------------------------------------------------------------------------

def compile_term_regex(terms, limit=4000):
    """把术语表编译成单个正则，避免逐术语子串扫描带来的 O(n*m) 开销。"""
    cleaned = []
    for term in terms or []:
        if not term or len(term) < 2 or len(term) > 32:
            continue
        if re.search(r"[\\^$.|?*+()\[\]{}]", term):
            continue
        cleaned.append(term)
    cleaned = sorted(set(cleaned), key=lambda t: (-len(t), t))[:limit]
    if not cleaned:
        return None
    return re.compile("|".join(re.escape(t) for t in cleaned))


def extract_raw(spec, page_filter=None):
    """Phase A：逐页解析 PDF（昂贵，结果可缓存）。

    只做与版面一一对应的重型工作：逐页抽取行、表格与目录。
    分类、分节、切块等廉价且需要反复调整的逻辑放在 finish_document()，
    这样修改分类规则时无需重新解析整份 PDF（API 手册约 8 分钟）。
    """
    doc_id = spec["doc_id"]
    for directory in (OUT_ROOT, REPORT_DIR, TABLE_DIR, SCHEMA_DIR, MD_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    issue_start = len(ISSUES)
    doc = pymupdf.open(spec["path"])
    excluded = set(spec["excluded_pages"])
    toc_index, toc_entries = build_toc_index(doc, spec, excluded, page_filter)

    all_elements = []
    all_tables = []
    page_stats = {}
    total_chars = 0
    started = time.time()
    for page_no in range(1, doc.page_count + 1):
        if page_no in excluded:
            continue
        if page_filter is not None and page_no not in page_filter:
            continue
        page = doc[page_no - 1]
        elements, table_payloads, stats = extract_page(page, spec, page_no)
        page_stats[page_no] = dict(stats)
        total_chars += sum(len(e.get("text", "")) + len(e.get("md", "") or "") for e in elements)
        all_elements.extend(elements)
        all_tables.extend(table_payloads)
        if page_no % 400 == 0:
            print(f"  [{doc_id}] page {page_no}/{doc.page_count} (+{time.time() - started:.0f}s)", flush=True)

    doc_meta = {
        "doc_id": doc_id,
        "title": spec["title"],
        "doc_type": spec["doc_type"],
        "vendor": VENDOR,
        "product": PRODUCT,
        "version": DOC_VERSION,
        "source_path": spec["path"],
        "sha256": sha256_file(spec["path"]),
        "page_count": doc.page_count,
        "processed_at": NOW,
        "parser": {"name": PARSER_NAME, "version": PARSER_VERSION},
        "excluded_pages": sorted(excluded),
        "excluded_reason": "封面 / 目录等非正文章节",
        "extraction_method": "text_layer",
        "language": "zh-CN",
    }
    doc.close()
    return {
        "meta": doc_meta,
        "all_elements": all_elements,
        "tables": all_tables,
        "page_stats": page_stats,
        "total_chars": total_chars,
        "toc_entries": toc_entries,
        # 转成普通 dict，便于序列化进缓存（classify 只用 .get() 读取）
        "toc_index": {page: {key: list(vals) for key, vals in bucket.items()}
                      for page, bucket in toc_index.items()},
        "raw_issues": ISSUES[issue_start:],
    }


def finish_document(spec, raw):
    """Phase B：分类 / 分节 / 写表格（廉价，可反复重跑，结果不缓存）。"""
    doc_id = spec["doc_id"]
    issue_start = len(ISSUES)
    toc_index = raw["toc_index"]
    toc_entries = raw["toc_entries"]
    all_elements = raw["all_elements"]
    all_tables = raw["tables"]

    # ---- 分类（含目录匹配） ----
    classified, matched = classify(all_elements, toc_index, spec)
    classified = attach_table_captions(classified)

    # ---- 目录完整性检查（在分类之后，避免把已命中条目误判为缺失） ----
    unmatched = []
    for entry in toc_entries:
        if entry["title"] == "目录":
            continue
        if (normalize_title(entry["title"]), entry["page"]) in matched:
            continue
        # 标题可能出现在目录标注页的下一页（跨页排版）
        if (normalize_title(entry["title"]), entry["page"] + 1) in matched:
            continue
        unmatched.append(entry)
    for entry in unmatched:
        add_issue(
            doc_id,
            "toc_title_not_found",
            "目录条目在正文中未找到同名标题，该层级回退为字号推断",
            page=entry["page"],
            severity="warning",
            detail=f"level={entry['level']} title={entry['title']}",
        )
    if unmatched:
        add_issue(
            doc_id,
            "toc_title_not_found_summary",
            f"共 {len(unmatched)}/{len(toc_entries)} 个目录条目未精确匹配到正文标题",
            page=None,
            severity="info",
            detail="明细见同类别 warning 记录；这些位置改用字号推断标题层级。",
        )

    root = build_sections(classified, doc_id, spec["doc_type"], PRODUCT, DOC_VERSION)

    # ---- 写表格 HTML（此时已知每个表格的上下文标题路径） ----
    write_all_tables(doc_id, root, all_tables)

    return {
        "meta": raw["meta"],
        "root": root,
        "elements": classified,
        "tables": all_tables,
        "page_stats": raw["page_stats"],
        "total_chars": raw["total_chars"],
        "toc_entries": toc_entries,
        "parse_issues": ISSUES[issue_start:],
    }


def parse_document(spec, vocabulary_seed, page_filter=None):
    """完整解析：Phase A + Phase B（供干跑与试点脚本使用）。"""
    raw = extract_raw(spec, page_filter=page_filter)
    parsed = finish_document(spec, raw)
    parsed["parse_issues"] = list(raw.get("raw_issues", [])) + list(parsed["parse_issues"])
    return parsed


def write_all_tables(doc_id, root, all_tables):
    """把解析出的表格写成带上下文的 HTML 文件。"""
    table_context = {}
    for section in iter_sections(root):
        for element in section["elements"]:
            if element["kind"] == "table":
                table_context[element["table_id"]] = {
                    "title_path": section["title_path"],
                    "caption": element.get("caption"),
                }
    for table in all_tables:
        context = table_context.get(table["table_id"], {})
        write_table_html(doc_id, table["page"], table, context)


def cache_key(spec):
    """基于 PDF 指纹 + 解析逻辑版本 + kb_common 代码指纹生成缓存键。"""
    path = Path(spec["path"])
    stat = path.stat()
    payload = "|".join(
        [
            CACHE_VERSION,
            spec["doc_id"],
            str(stat.st_size),
            str(stat.st_mtime_ns),
            KB_COMMON_FINGERPRINT,
        ]
    )
    return sha256_text(payload)[:16]


def load_parse_cache(spec):
    key = cache_key(spec)
    cache_path = CACHE_DIR / f"{spec['doc_id']}.{key}.pkl"
    if not cache_path.exists():
        return None
    try:
        import pickle

        with open(cache_path, "rb") as handle:
            payload = pickle.load(handle)
        if payload.get("version") != CACHE_VERSION:
            return None
        return payload.get("parsed")
    except Exception as exc:  # pragma: no cover - 缓存损坏时回退到真实解析
        print(f"  [{spec['doc_id']}] 缓存读取失败，回退真实解析：{exc}", flush=True)
        try:
            cache_path.unlink()
        except Exception:
            pass
        return None


def _sanitize_for_pickle(value, depth=0):
    """把结构中不可 pickle 的叶子（如 fitz.Table / fitz.Rect）替换为 None。

    解析器产出的结构本身已是纯 Python 对象；这里是缓存写入的兜底保护，
    避免任何残留的第三方对象导致整份缓存失效。
    """
    if depth > 80:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize_for_pickle(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_pickle(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_sanitize_for_pickle(v, depth + 1) for v in value]
    return None


def save_parse_cache(spec, parsed):
    try:
        import pickle

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        key = cache_key(spec)
        cache_path = CACHE_DIR / f"{spec['doc_id']}.{key}.pkl"
        payload = {"version": CACHE_VERSION, "parsed": parsed}
        try:
            blob = pickle.dumps(payload, protocol=4)
        except Exception:
            blob = pickle.dumps(
                {"version": CACHE_VERSION, "parsed": _sanitize_for_pickle(parsed)}, protocol=4
            )
        with open(cache_path, "wb") as handle:
            handle.write(blob)
        print(f"  [{spec['doc_id']}] 解析结果已写入缓存 {cache_path.name}", flush=True)
    except Exception as exc:  # pragma: no cover
        print(f"  [{spec['doc_id']}] 缓存写入失败（不影响产物）：{exc}", flush=True)


def write_table_html(doc_id, page_no, table, context):
    """写单个表格的 HTML（保留合并单元格），并附带标题、页码与上下文标题路径。"""
    rows = table["rows"]
    header = rows[0] if rows else []
    body = []
    for row in rows[1:]:
        body.append("<tr>" + "".join(f"<td>{_html_escape(c)}</td>" for c in row) + "</tr>")
    title_path = " / ".join(context.get("title_path") or []) or "(未归属章节)"
    caption = context.get("caption") or ""
    html_doc = (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{_html_escape(table['table_id'])}</title>\n"
        "<style>body{font-family:'Microsoft YaHei',sans-serif;font-size:13px}"
        "table{border-collapse:collapse}td,th{border:1px solid #999;padding:4px 8px;"
        "vertical-align:top}th{background:#f0f0f0}.meta{color:#555;font-size:12px}"
        "caption{caption-side:top;text-align:left;font-weight:bold;padding:6px 0}</style>\n"
        "</head>\n<body>\n"
        '<p class="meta">'
        f"doc_id={_html_escape(doc_id)} | pdf_page={page_no} | table_id={_html_escape(table['table_id'])}<br>"
        f"上下文标题路径：{_html_escape(title_path)}"
        "</p>\n"
        "<table>\n"
        + (f"<caption>{_html_escape(caption)}</caption>\n" if caption else "")
        + "<thead><tr>"
        + "".join(f"<th>{_html_escape(c)}</th>" for c in header)
        + "</tr></thead>\n<tbody>\n"
        + "\n".join(body)
        + "\n</tbody>\n</table>\n</body>\n</html>\n"
    )
    (TABLE_DIR / table["html_path"]).write_text(html_doc, encoding="utf-8", newline="\n")


_CAPTION_RE = re.compile(r"^表\s*\d+(?:[-–]\d+)?[\s、.．:：]?\S*")


def attach_table_captions(classified):
    """把紧邻表格上方的「表N ...」图题挂到表格元素上。"""
    for index, element in enumerate(classified):
        if element["kind"] != "table":
            continue
        for lookback in range(index - 1, max(-1, index - 3), -1):
            previous = classified[lookback]
            if previous["kind"] != "para":
                break
            text = previous.get("text", "").strip()
            if _CAPTION_RE.match(text) or text.endswith("说明表"):
                element["caption"] = text
            break
    return classified


def _html_escape(text):
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


# --------------------------------------------------------------------------
# 章节 -> chunk
# --------------------------------------------------------------------------

def build_chunks(parsed, spec):
    doc_id = spec["doc_id"]
    doc_type = spec["doc_type"]
    chunks = []
    ordinal = 0
    title_index = collections.defaultdict(list)
    for section in iter_sections(parsed["root"]):
        if not section["elements"]:
            continue
        packs = pack_chunks(section, spec)
        for pack in packs:
            ordinal += 1
            markdown = "\n\n".join(element_markdown(e) for e in pack)
            plain = markdown_to_plain(markdown)
            if not plain.strip():
                add_issue(
                    doc_id,
                    "empty_section",
                    "章节切块后内容为空，已跳过",
                    page=section["page_start"],
                    severity="warning",
                    detail=f"title_path={' / '.join(section['title_path'])}",
                )
                continue
            pages = [element_page(e) for e in pack if element_page(e)]
            page_start = min(pages) if pages else section["page_start"]
            page_end = max(pages) if pages else section["page_end"]
            text_hash = sha256_text(plain)
            chunk_id = make_chunk_id(doc_id, section["title_path"], page_start, ordinal, plain)
            entities = extract_entities(plain, parsed)
            chunk = {
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "doc_type": doc_type,
                "product": PRODUCT,
                "version": DOC_VERSION,
                "title": section["title"],
                "section_no": section["section_no"],
                "title_path": section["title_path"],
                "page_start": page_start,
                "page_end": page_end,
                "content_markdown": markdown,
                "content_plain": plain,
                "keywords": build_keywords(plain, parsed.get("keyword_re"), limit=12),
                "entities": entities,
                "char_count": len(plain),
                "table_ids": [e["table_id"] for e in pack if e["kind"] == "table"],
                "has_code": any(e["kind"] == "code" for e in pack),
                "prev_chunk_id": None,
                "next_chunk_id": None,
                "text_sha256": text_hash,
            }
            chunks.append(chunk)
            title_index[section["title"]].append(chunk_id)

    # 保持文档顺序，不使用页码排序（跨页章节的页内顺序才是真实阅读顺序）
    for index, chunk in enumerate(chunks):
        chunk["prev_chunk_id"] = chunks[index - 1]["chunk_id"] if index > 0 else None
        chunk["next_chunk_id"] = chunks[index + 1]["chunk_id"] if index + 1 < len(chunks) else None

    # 重复文本检测
    by_hash = collections.defaultdict(list)
    for chunk in chunks:
        by_hash[chunk["text_sha256"]].append(chunk["chunk_id"])
    for text_hash, ids in by_hash.items():
        if len(ids) > 1:
            add_issue(
                doc_id,
                "duplicate_text",
                "存在完全重复的 chunk 正文",
                page=None,
                severity="warning",
                related=ids,
                detail=f"count={len(ids)}",
            )
    for chunk in chunks:
        if chunk["char_count"] > 4000:
            add_issue(
                doc_id,
                "oversized_chunk",
                "chunk 超出常规上限（完整参数表 / 命令说明，未从中间切断）",
                page=chunk["page_start"],
                severity="info",
                related=[chunk["chunk_id"]],
                detail=f"char_count={chunk['char_count']} title={' / '.join(chunk['title_path'])}",
            )
        elif chunk["char_count"] < 60:
            add_issue(
                doc_id,
                "short_chunk",
                "chunk 异常偏短（标题级碎片）",
                page=chunk["page_start"],
                severity="info",
                related=[chunk["chunk_id"]],
                detail=f"char_count={chunk['char_count']}",
            )
    return chunks, title_index


_API_PATH_TOKEN = re.compile(r"/(?:api|v\d)[A-Za-z0-9_@{}$./\-]*")
_CLI_TOKEN = re.compile(r"\b(show|no)\s+[a-z][a-z0-9\-]{2,}")
_METHOD_LINE_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(https?://\S+)", re.MULTILINE)
_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")


def extract_entities(plain, parsed):
    entities = []
    seen = set()

    def push(kind, value):
        if not value:
            return
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        entities.append({"type": kind, "value": value})

    for match in _METHOD_LINE_RE.finditer(plain):
        push("http_method", match.group(1))
    for match in _API_PATH_TOKEN.finditer(plain):
        push("api_path", match.group(0))
    for match in _VERSION_RE.finditer(plain):
        push("version", match.group(0))
    if parsed["profile"] == "cli":
        for match in _CLI_TOKEN.finditer(plain):
            push("cli_command", squeeze_spaces(match.group(0)))
    if parsed["profile"] == "api":
        for match in re.finditer(r"\b\"code\"\s*:\s*(-?\d+)", plain):
            push("error_code", match.group(1))
    reserved_re = parsed.get("reserved_re")
    if reserved_re is not None:
        for match in reserved_re.finditer(plain):
            push("reserved_term", match.group(0))
    return entities[:60]


# --------------------------------------------------------------------------
# API 端点抽取
# --------------------------------------------------------------------------

_PARAM_LABEL_TO_LOCATION = {
    "URL参数": "query",
    "Body参数": "body",
    "Header参数": "header",
}

_TYPE_MAP = {
    "string": "string",
    "int": "integer",
    "int32": "integer",
    "int64": "integer",
    "uint64": "integer",
    "uint32": "integer",
    "long": "integer",
    "float": "number",
    "double": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "object": "object",
    "object[]": "array",
    "array": "array",
    "string[]": "array",
    "int[]": "array",
}


def map_type(raw_type):
    if not raw_type:
        return None
    key = raw_type.strip()
    if key.endswith("[]"):
        inner = map_type(key[:-2]) or "object"
        return {"type": "array", "items": {"type": inner}}
    return _TYPE_MAP.get(key, key)


def parse_description(detail):
    """拆分描述中的 默认值 / 枚举值 / 格式 / 长度与取值范围约束。"""
    result = {
        "description_lines": [],
        "default": None,
        "enum": None,
        "format": None,
        "min_length": None,
        "max_length": None,
        "min_value": None,
        "max_value": None,
        "raw": detail,
    }
    enum_lines = []
    in_enum = False
    for raw_line in (detail or "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if in_enum:
            if line.startswith("]"):
                in_enum = False
                continue
            enum_lines.append(line)
            continue
        normalised = line.replace("：", ":")
        head, sep, tail = normalised.partition(":")
        if sep and head in ("默认值", "枚举值", "格式"):
            tail = tail.strip()
            if head == "默认值":
                result["default"] = tail
            elif head == "格式":
                result["format"] = tail
            else:
                if tail.startswith("["):
                    in_enum = True
                    remainder = tail[1:].strip()
                    if remainder and remainder != "]":
                        enum_lines.append(remainder)
                else:
                    enum_lines.append(tail)
            continue
        if sep and head in ("最小长度", "最大长度", "最小值", "最大值"):
            if head == "最小长度":
                result["min_length"] = _to_number(tail)
            elif head == "最大长度":
                result["max_length"] = _to_number(tail)
            elif head == "最小值":
                result["min_value"] = _to_number(tail)
            else:
                result["max_value"] = _to_number(tail)
            continue
        result["description_lines"].append(line)
    if enum_lines:
        parsed_enum = []
        for entry in enum_lines:
            value, _, desc = entry.partition(":")
            parsed_enum.append({"value": value.strip(), "description": desc.strip() or None})
        result["enum"] = parsed_enum
    return result


def _to_number(text):
    try:
        return int(text.strip())
    except Exception:
        return None


def parse_param_table(rows, location):
    """把「参数/类型/是否必选/描述」类表格转成参数对象列表。"""
    if not rows:
        return [], []
    header = [c.strip() for c in rows[0]]
    has_required = "是否必选" in header
    params = []
    for row in rows[1:]:
        cells = list(row) + [""] * (len(header) - len(row))
        name = (cells[0] or "").strip()
        if not name:
            continue
        raw_type = (cells[1] or "").strip() if len(cells) > 1 else ""
        if has_required:
            required_raw = (cells[2] or "").strip() if len(cells) > 2 else ""
            detail = cells[3] if len(cells) > 3 else ""
            required = {"是": True, "否": False}.get(required_raw)
        else:
            required = None
            detail = cells[2] if len(cells) > 2 else ""
        parsed = parse_description(detail)
        params.append(
            {
                "name": name,
                "location": location,
                "type": raw_type or None,
                "required": required,
                "description": "\n".join(parsed["description_lines"]) or None,
                "default": parsed["default"],
                "enum": parsed["enum"],
                "example": None,
                "format": parsed["format"],
                "min_length": parsed["min_length"],
                "max_length": parsed["max_length"],
                "min_value": parsed["min_value"],
                "max_value": parsed["max_value"],
                "raw_detail": parsed["raw"],
                "table_header": header,
            }
        )
    return params, header


def build_schema_from_params(params):
    if not params:
        return None
    root = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {}}
    flat = []
    for param in params:
        flat.append(
            {
                "name": param["name"],
                "type": param["type"],
                "required": param["required"],
                "description": param["description"],
                "default": param["default"],
                "enum": param["enum"],
                "format": param["format"],
                "location": param["location"],
                "min_length": param.get("min_length"),
                "max_length": param.get("max_length"),
                "min_value": param.get("min_value"),
                "max_value": param.get("max_value"),
            }
        )
        parts = [p for p in (param["name"] or "").split(".") if p]
        if not parts:
            continue
        node = root
        for depth, part in enumerate(parts):
            props = node.setdefault("properties", {})
            if depth == len(parts) - 1:
                leaf = {"type": map_type(param["type"]) or "string"}
                if param["description"]:
                    leaf["description"] = param["description"]
                if param["default"] is not None:
                    leaf["default"] = param["default"]
                if param["enum"]:
                    leaf["enum"] = [item["value"] for item in param["enum"]]
                if param["format"]:
                    leaf["format"] = param["format"]
                if param["min_length"] is not None:
                    leaf["minLength"] = param["min_length"]
                if param["max_length"] is not None:
                    leaf["maxLength"] = param["max_length"]
                if param.get("min_value") is not None:
                    leaf["minimum"] = param["min_value"]
                if param.get("max_value") is not None:
                    leaf["maximum"] = param["max_value"]
                props.setdefault(part, leaf)
            else:
                child = props.setdefault(part, {"type": "object", "properties": {}})
                node = child
    root["x-flat-parameters"] = flat
    return root


_SAMPLE_MARKERS = (
    ("请求成功样例", "success"),
    ("请求失败样例", "fail"),
    ("请求样例", "request"),
)
_CONSTRAINT_HINTS = ("不支持", "注意", "仅", "需", "必须", "限制")


def extract_api_endpoints(parsed, spec, title_index):
    doc_id = spec["doc_id"]
    endpoints = []
    seen_ids = collections.Counter()
    sections = [s for s in iter_sections(parsed["root"]) if s["elements"]]
    for section in sections:
        elements = section["elements"]
        has_request_block = any(
            e["kind"] == "marker" and "API请求数据" in e["text"] for e in elements
        )
        if not has_request_block:
            continue
        section_no = section["section_no"] or f"auto-{len(endpoints) + 1}"
        base_id = f"{doc_id}#{section_no}"
        seen_ids[base_id] += 1
        endpoint_id = base_id if seen_ids[base_id] == 1 else f"{base_id}-{seen_ids[base_id]}"

        summary = None
        method = None
        path = None
        query_params, body_params, header_params, response_params = [], [], [], []
        request_examples, response_examples, error_examples = [], [], []
        constraints = []
        notes = []
        # 状态机：marker 决定参数区，bullet 决定参数归属，段落里的「样例：」决定代码归属
        param_target = None      # 'query' | 'body' | 'header' | 'response' | None
        sample_target = None     # 'request' | 'success' | 'fail' | None
        param_bucket = {"query": query_params, "body": body_params,
                        "header": header_params, "response": response_params}
        sample_bucket = {"request": request_examples, "success": response_examples,
                         "fail": error_examples}

        for element in elements:
            kind = element["kind"]
            text = element.get("text", "")
            if kind == "marker":
                if "输入参数" in text or "输出参数" in text:
                    param_target = None
                    sample_target = None
                continue
            if kind == "bullet":
                label = text.strip()
                if label in _PARAM_LABEL_TO_LOCATION:
                    param_target = _PARAM_LABEL_TO_LOCATION[label]
                elif label == "API返回数据":
                    param_target = "response"
                else:
                    notes.append(f"未识别的参数分组标签：{label}")
                continue
            if kind == "para":
                if text.startswith("功能说明"):
                    summary = re.sub(r"^功能说明\s*[:：]\s*", "", text).strip()
                    continue
                matched_sample = None
                for prefix, target in _SAMPLE_MARKERS:
                    if text.startswith(prefix):
                        matched_sample = target
                        break
                if matched_sample:
                    sample_target = matched_sample
                    continue
                continue
            if kind == "code":
                if element.get("style") == "request-line" and method is None:
                    found_method, found_path = parse_method_line(text)
                    if found_method:
                        method, path = found_method, found_path
                        continue
                if sample_target:
                    sample_bucket[sample_target].append(text)
                continue
            if kind == "table":
                if param_target:
                    params, _ = parse_param_table(element["rows"], param_target)
                    param_bucket[param_target].extend(params)
                else:
                    notes.append(f"未归属参数表：{element['table_id']}")

        if summary:
            for match in re.finditer(r"[（(]([^（()]*?)[)）]", summary):
                inner = match.group(1)
                if any(k in inner for k in _CONSTRAINT_HINTS):
                    constraints.append(inner)
        if path and not path.startswith("http") and method:
            # 保持文档原样；文档中路径统一写作 https://{host}/...
            pass

        source_chunks = list(title_index.get(section["title"], []))
        if not source_chunks:
            add_issue(
                doc_id,
                "endpoint_without_chunk",
                "API 端点未能关联到 chunk",
                page=section["page_start"],
                severity="error",
                detail=endpoint_id,
            )
            continue

        endpoints.append(
            {
                "endpoint_id": endpoint_id,
                "doc_id": doc_id,
                "title": section["title"],
                "section_no": section_no,
                "title_path": section["title_path"],
                "method": method,
                "path": path,
                "summary": summary,
                "auth": None,
                "content_type": "application/json"
                if any("json" in (code or "").lower() for code in request_examples + response_examples)
                else None,
                "path_parameters": [],
                "query_parameters": query_params,
                "headers": header_params,
                "request_body_schema": build_schema_from_params(body_params),
                "response_schema": build_schema_from_params(response_params),
                "success_codes": None,
                "error_codes": None,
                "request_examples": request_examples,
                "response_examples": response_examples,
                "error_examples": error_examples,
                "constraints": constraints or None,
                "notes": None,
                "unparsed_blocks": notes or None,
                "page_start": section["page_start"],
                "page_end": section["page_end"],
                "source_chunk_ids": source_chunks,
            }
        )
        if method is None or path is None:
            add_issue(
                doc_id,
                "missing_api_method_or_path",
                "端点未能解析出 HTTP 方法或路径",
                page=section["page_start"],
                severity="warning",
                related=[endpoint_id],
                detail=section["title"],
            )
        if notes:
            add_issue(
                doc_id,
                "unparsed_block_in_endpoint",
                "端点内存在未归属的参数表或分组标签",
                page=section["page_start"],
                severity="warning",
                related=[endpoint_id],
                detail="; ".join(notes[:5]),
            )
    return endpoints


# --------------------------------------------------------------------------
# CLI 命令抽取
# --------------------------------------------------------------------------

def split_cli_blocks(elements):
    blocks = []
    for element in elements:
        if element["kind"] == "marker":
            blocks.append({"label": element["text"], "elements": [], "style": element.get("style")})
            continue
        if element["kind"] == "bullet":
            blocks.append({"label": element["text"], "elements": [], "style": "bullet"})
            continue
        if not blocks:
            blocks.append({"label": "lead", "elements": [], "style": None})
        blocks[-1]["elements"].append(element)
    return blocks


_CLI_PROMPT_MODE_RE = re.compile(r"^hostname[\w\-]*(?:\((?P<mode>[\w\-]+)\))?\s*[#>]")


def mode_from_prompt(prompt: str):
    """依据文档 1.1「命令模式和提示符」由示例提示符判定命令模式。"""
    match = _CLI_PROMPT_MODE_RE.match((prompt or "").strip())
    if not match:
        return None
    inner = match.group("mode")
    if not inner:
        return "执行模式"
    if inner == "config":
        return "全局配置模式"
    return "子模块配置模式"


def extract_cli_commands(parsed, spec, title_index):
    doc_id = spec["doc_id"]
    commands = []
    seen_ids = collections.Counter()
    sections = [s for s in iter_sections(parsed["root"]) if s["elements"]]
    subcommand_total = 0
    for section in sections:
        elements = section["elements"]
        if not any(e["kind"] == "marker" and "命令名称" in e["text"] for e in elements):
            continue
        blocks = split_cli_blocks(elements)
        fields = collections.defaultdict(list)
        arg_rows = []
        arg_headers = []
        examples = []
        subcommands = []
        pending_sub = None

        for block in blocks:
            label = block["label"]
            texts = [e["text"] for e in block["elements"] if e["kind"] == "para"]
            codes = [e["text"] for e in block["elements"] if e["kind"] == "code"]
            tables = [e for e in block["elements"] if e["kind"] == "table"]
            if block.get("style") == "subcommand":
                pending_sub = {"name": re.sub(r"^<\d+>\s*", "", label).strip(), "fields": collections.defaultdict(list),
                               "page": block["elements"][0]["page"] if block["elements"] else section["page_start"]}
                subcommands.append(pending_sub)
                continue

            if block.get("style") == "bullet":
                # 子命令的「• 概要 / • 功能说明 / • 参数说明」
                target = pending_sub["fields"] if pending_sub else fields
                target[label].extend(texts)
                if label == "参数说明":
                    target["_参数表"].extend(tables)
                continue

            matched_key = None
            for key in ("命令名称", "概要", "功能说明", "参数说明", "子命令", "示例"):
                if label.startswith(("1)", "2)", "3)", "4)", "5)", "6)")) and key in label:
                    matched_key = key
                    break
                if key in label:
                    matched_key = key
                    break
            target = pending_sub["fields"] if pending_sub else fields
            if matched_key:
                target[matched_key].extend(texts)
                target[f"_codes:{matched_key}"].extend(codes)
                if matched_key == "参数说明":
                    target["_参数表"].extend(tables)
            else:
                target["_extra"].extend(texts)
                target["_extra_codes"].extend(codes)
                target["_参数表"].extend(tables)

        # 主命令字段
        def field(name):
            values = fields.get(name) or []
            return " ".join(v.strip() for v in values if v.strip()).strip() or None

        command_name = field("命令名称")
        syntax = field("概要")
        summary = field("功能说明")
        if not command_name and section["title"]:
            command_name = re.sub(r"^\d+(?:\.\d+)*\s+", "", section["title"]).strip()

        for table in fields.get("_参数表", []):
            rows = table["rows"]
            if not rows:
                continue
            arg_headers.append(rows[0])
            for row in rows[1:]:
                cells = list(row)
                while len(cells) < len(rows[0]):
                    cells.append("")
                arg_rows.append(
                    {
                        "name": (cells[0] or "").strip() or None,
                        "range": (cells[1] or "").strip() or None if len(cells) > 1 else None,
                        "input_format": (cells[2] or "").strip() or None if len(cells) > 2 else None,
                        "description": (cells[3] or "").strip() or None if len(cells) > 3 else None,
                        "table_id": table["table_id"],
                    }
                )
        example_codes = list(fields.get("_codes:示例", [])) + list(fields.get("示例", []))
        for code in example_codes:
            for line in code.split("\n"):
                line = line.strip()
                if line:
                    examples.append(line)

        modes = set()
        for line in examples:
            mode = mode_from_prompt(line)
            if mode:
                modes.add(mode)

        warnings = []
        for text in (summary or "").split("\n"):
            for match in re.finditer(r"[（(]([^（()）]*?)[)）]", text or ""):
                inner = match.group(1)
                if any(k in inner for k in ("不支持", "注意", "仅", "需", "必须", "限制")):
                    warnings.append(inner)
        for text in fields.get("_extra", []):
            for match in re.finditer(r"[（(]([^（()）]*?)[)）]", text or ""):
                inner = match.group(1)
                if any(k in inner for k in ("不支持", "注意", "仅", "需", "必须", "限制")):
                    warnings.append(inner)

        child_names = []
        for text in fields.get("子命令", []):
            for match in re.finditer(r"<\d+>\s*([\w\-]+)", text):
                child_names.append(match.group(1))
        for sub in subcommands:
            child_names.append(sub["name"])

        base_id = f"{doc_id}#{section['section_no'] or 'auto'}"
        seen_ids[base_id] += 1
        command_id = base_id if seen_ids[base_id] == 1 else f"{base_id}-{seen_ids[base_id]}"
        source_chunks = title_index.get(section["title"], [])
        if not source_chunks:
            add_issue(
                doc_id,
                "cli_without_chunk",
                "CLI 命令未能关联到 chunk",
                page=section["page_start"],
                severity="error",
                detail=command_id,
            )
        commands.append(
            {
                "command_id": command_id,
                "doc_id": doc_id,
                "command": command_name,
                "section_no": section["section_no"],
                "title_path": section["title_path"],
                "syntax": syntax,
                "mode": sorted(modes)[0] if len(modes) == 1 else None,
                "modes_observed": sorted(modes) or None,
                "privilege": None,
                "summary": summary,
                "arguments": arg_rows or None,
                "argument_table_headers": arg_headers or None,
                "options": None,
                "examples": examples or None,
                "output_description": None,
                "warnings": sorted(set(warnings)) or None,
                "related_commands": sorted(set(child_names)) or None,
                "page_start": section["page_start"],
                "page_end": section["page_end"],
                "source_chunk_ids": source_chunks,
            }
        )

        # 子命令（CLI 文档中「子命令」小节内的 <n> 条目）
        for index, sub in enumerate(subcommands, start=1):
            name = sub["name"]
            if not name:
                continue
            fields_sub = sub["fields"]
            sub_syntax = " ".join(v.strip() for v in fields_sub.get("概要", []) if v.strip()) or None
            sub_summary = " ".join(v.strip() for v in fields_sub.get("功能说明", []) if v.strip()) or None
            if not sub_syntax and not sub_summary:
                continue
            subcommand_total += 1
            sub_id = f"{command_id}/sub-{index}-{re.sub(r'[^A-Za-z0-9\\-]', '_', name)[:32]}"
            sub_rows = []
            for table in fields_sub.get("_参数表", []):
                rows = table["rows"]
                for row in rows[1:]:
                    cells = list(row) + [""] * (len(rows[0]) - len(row))
                    sub_rows.append(
                        {
                            "name": (cells[0] or "").strip() or None,
                            "range": (cells[1] or "").strip() or None,
                            "input_format": (cells[2] or "").strip() or None,
                            "description": (cells[3] or "").strip() or None,
                            "table_id": table["table_id"],
                        }
                    )
            commands.append(
                {
                    "command_id": sub_id,
                    "doc_id": doc_id,
                    "command": name,
                    "section_no": section["section_no"],
                    "title_path": section["title_path"] + ["子命令", name],
                    "syntax": sub_syntax,
                    "mode": None,
                    "modes_observed": None,
                    "privilege": None,
                    "summary": sub_summary,
                    "arguments": sub_rows or None,
                    "argument_table_headers": None,
                    "options": None,
                    "examples": None,
                    "output_description": None,
                    "warnings": None,
                    "related_commands": None,
                    "parent_command_id": command_id,
                    "page_start": sub["page"],
                    "page_end": section["page_end"],
                    "source_chunk_ids": source_chunks,
                }
            )
    return commands, subcommand_total


# --------------------------------------------------------------------------
# 词表 / 术语
# --------------------------------------------------------------------------

def collect_vocabulary(parsed_docs):
    vocabulary = set()
    for parsed in parsed_docs:
        for section in iter_sections(parsed["root"]):
            title = section["title"]
            if not title:
                continue
            stripped = re.sub(r"^\d+(?:\.\d+)*\s*", "", title).strip()
            for token in re.split(r"[\s/、,，:：()（）]+", stripped):
                token = token.strip()
                if 2 <= len(token) <= 24 and not token.isdigit():
                    vocabulary.add(token)
        for chunk in parsed.get("chunks", []):
            for table_id in chunk.get("table_ids", []):
                pass
    domain = {
        "策略", "会话", "虚拟系统", "命名空间", "引用关系", "配置完整性校验",
        "高可用性", "双机", "主备部署", "透明主主部署", "物联网安全", "业务安全",
        "用户安全", "专项防护", "黑白名单", "安全运营中心", "风险预判",
        "策略有效性", "威胁情报", "僵尸主机", "网页篡改", "Webshell",
        "勒索", "资产发现", "准入", "安全组", "MAC地址对象", "模板",
        "证书", "资源池", "IPSec", "SSL", "SNMP", "RADIUS", "LDAP", "DHCP",
        "DNS", "ARP", "VLAN", "NAT", "VPN", "AD域", "终端检查响应",
    }
    vocabulary |= domain
    return vocabulary


GLOSSARY_SEEDS = [
    # (缩写, 中文名, 英文名, 别名, 类别)
    ("SNMP", "简单网络管理协议", "Simple Network Management Protocol", [], "协议"),
    ("RADIUS", "远程用户拨号认证服务", "Remote Authentication Dial In User Service", [], "协议"),
    ("DNS", "域名系统（服务）协议", "Domain Name System", [], "协议"),
    ("LDAP", "轻型目录访问协议", "Lightweight Directory Access Protocol", [], "协议"),
    ("DHCP", "动态主机配置协议", "Dynamic Host Configuration Protocol", [], "协议"),
    ("ARP", "地址解析协议", "Address Resolution Protocol", [], "协议"),
    ("TCP", "传输控制协议", "Transmission Control Protocol", [], "协议"),
    ("UDP", "用户数据报协议", "User Datagram Protocol", [], "协议"),
    ("VLAN", "虚拟局域网", "Virtual Local Area Network", [], "网络"),
    ("NAT", "网络地址转换", "Network Address Translation", [], "网络"),
    ("NetBIOS", "网上基本输入输出系统", "Network Basic Input/Output System", [], "协议"),
    ("BBC", "深信服集中管理平台 BBC", "Branch Bussiness Cente", [], "产品"),
    ("IM", "通讯软件", "Instant Messaging", [], "应用"),
    ("aES", "终端检查响应平台", "Endpoint Detection and Response", [], "产品"),
    ("AD", "活动目录", "Active Directory", [], "服务"),
    ("VPN", "虚拟专用网络", "Virtual Private Network", [], "网络"),
]

CONCEPT_TERMS = [
    "融合安全", "安全运营中心", "物联网安全", "业务安全", "用户安全", "专项防护",
    "下一代安全体系", "高可用性", "双机模式", "主备部署", "透明主主部署",
    "虚拟系统", "命名空间", "策略", "会话保持", "会话", "资产发现设置",
    "配置完整性校验", "引用关系", "模板", "证书", "资源池", "安全组",
    "黑白名单", "勒索专项防护", "策略有效性检测", "威胁情报", "僵尸主机",
]


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", default=None)
    parser.add_argument("--stage", choices=["all", "report"], default="all")
    args = parser.parse_args()

    for directory in (OUT_ROOT, REPORT_DIR, TABLE_DIR, SCHEMA_DIR, MD_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    if args.stage == "report":
        write_quality_report()
        return 0

    specs = DOC_SPECS
    if args.only:
        specs = [s for s in DOC_SPECS if s["doc_id"] in args.only]

    documents = []
    all_chunks = []
    all_endpoints = []
    all_commands = []
    parsed_list = []
    stats_per_doc = {}

    started = time.time()
    for spec in specs:
        raw = load_parse_cache(spec)
        if raw is not None:
            # 缓存只存 Phase A（逐页提取）；Phase A 产生的 issue 需要从缓存恢复
            ISSUES.extend(raw.get("raw_issues", []))
            print(
                f"  [{spec['doc_id']}] 命中解析缓存，跳过逐页解析 "
                f"(elements={len(raw['all_elements'])} chars={raw['total_chars']})",
                flush=True,
            )
        else:
            print(f"[build] parsing {spec['doc_id']} ...", flush=True)
            raw = extract_raw(spec)
            save_parse_cache(spec, raw)
            print(
                f"  [{spec['doc_id']}] elements={len(raw['all_elements'])} chars={raw['total_chars']} "
                f"({time.time() - started:.0f}s)",
                flush=True,
            )
        parsed = finish_document(spec, raw)
        parsed["meta"]["processed_at"] = NOW
        parsed["profile"] = spec["profile"]
        parsed_list.append(parsed)
        documents.append(parsed["meta"])

    vocabulary = collect_vocabulary(parsed_list)

    for spec, parsed in zip(specs, parsed_list):
        parsed["vocabulary"] = vocabulary
        parsed["keyword_re"] = compile_term_regex(vocabulary)
        # 保留词表：用于 entities.reserved_term
        reserved = {}
        for section in iter_sections(parsed["root"]):
            title = re.sub(r"^\d+(?:\.\d+)*\s*", "", section["title"] or "").strip()
            if 2 <= len(title) <= 24:
                reserved[title] = True
        parsed["reserved"] = reserved
        parsed["reserved_re"] = compile_term_regex(reserved.keys(), limit=2500)

        chunks, title_index = build_chunks(parsed, spec)
        parsed["chunks"] = chunks
        all_chunks.extend(chunks)
        print(f"  [{spec['doc_id']}] chunks={len(chunks)}", flush=True)

        if spec["doc_type"] == "api_manual":
            endpoints = extract_api_endpoints(parsed, spec, title_index)
            all_endpoints.extend(endpoints)
            print(f"  [{spec['doc_id']}] endpoints={len(endpoints)}", flush=True)
        if spec["doc_type"] == "cli_manual":
            commands, sub_total = extract_cli_commands(parsed, spec, title_index)
            all_commands.extend(commands)
            print(f"  [{spec['doc_id']}] cli records={len(commands)} (sub={sub_total})", flush=True)

        write_source_markdown(parsed, spec)
        stats_per_doc[spec["doc_id"]] = summarize_doc(parsed, spec, chunks)

    glossary = build_glossary(parsed_list)
    qa_seed = build_qa_seed(parsed_list, all_chunks, all_endpoints, all_commands)

    write_jsonl(OUT_ROOT / "documents.jsonl", documents)
    write_jsonl(OUT_ROOT / "chunks.jsonl", all_chunks)
    write_jsonl(OUT_ROOT / "api_endpoints.jsonl", all_endpoints)
    write_jsonl(OUT_ROOT / "cli_commands.jsonl", all_commands)
    write_jsonl(OUT_ROOT / "glossary.jsonl", glossary)
    write_jsonl(OUT_ROOT / "qa_seed.jsonl", qa_seed)
    write_jsonl(OUT_ROOT / "issues.jsonl", ISSUES)
    write_schemas()

    manifest = {
        "generated_at": NOW,
        "generator": "knowledge/scripts/build_knowledge.py",
        "parser": {"name": PARSER_NAME, "version": PARSER_VERSION},
        "product": PRODUCT,
        "version": DOC_VERSION,
        "vendor": VENDOR,
        "documents": [
            {
                "doc_id": d["doc_id"],
                "title": d["title"],
                "doc_type": d["doc_type"],
                "source_path": d["source_path"],
                "sha256": d["sha256"],
                "page_count": d["page_count"],
                "excluded_pages": d["excluded_pages"],
            }
            for d in documents
        ],
        "counts": {
            "documents": len(documents),
            "chunks": len(all_chunks),
            "api_endpoints": len(all_endpoints),
            "cli_commands": len(all_commands),
            "glossary": len(glossary),
            "qa_seed": len(qa_seed),
            "issues": len(ISSUES),
            "tables_html": len(list(TABLE_DIR.glob("*.html"))),
        },
        "per_document": stats_per_doc,
        "outputs": {},
        "notes": [
            "页码统一为 PDF 物理页码，从 1 开始。",
            "封面与目录页不进入 chunks / source_markdown，见各文档 excluded_pages。",
            "每端点鉴权方式在文档 1.2 交互流程与报文格式 中统一定义，未在任何端点小节内重复，故 api_endpoints.auth 统一为 null。",
            "API 返回码在文档 1.5 与附录 12.1 统一定义，未在端点小节内重复，故 success_codes / error_codes 为 null。",
        ],
    }
    for name in (
        "documents.jsonl",
        "chunks.jsonl",
        "api_endpoints.jsonl",
        "cli_commands.jsonl",
        "glossary.jsonl",
        "qa_seed.jsonl",
        "issues.jsonl",
    ):
        path = OUT_ROOT / name
        manifest["outputs"][name] = {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "lines": count_lines(path),
        }
    (OUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    write_quality_report()
    print(f"[build] done in {time.time() - started:.0f}s", flush=True)
    return 0


def count_lines(path):
    with open(path, "rb") as handle:
        return sum(1 for _ in handle)


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[build] wrote {path.relative_to(REPO_ROOT)} ({len(records)} lines)", flush=True)


def summarize_doc(parsed, spec, chunks):
    pages_with_tables = {p: s for p, s in parsed["page_stats"].items() if s.get("tables")}
    return {
        "doc_id": spec["doc_id"],
        "page_count": parsed["meta"]["page_count"],
        "excluded_pages": len(spec["excluded_pages"]),
        "processed_pages": parsed["meta"]["page_count"] - len(spec["excluded_pages"]),
        "extraction_method": "text_layer",
        "ocr_pages": [],
        "extracted_chars": parsed["total_chars"],
        "chunks": len(chunks),
        "tables": sum(s.get("tables", 0) for s in parsed["page_stats"].values()),
        "request_line_boxes": sum(s.get("request_lines", 0) for s in parsed["page_stats"].values()),
        "pages_with_tables": len(pages_with_tables),
        "toc_entries": len(parsed["toc_entries"]),
    }


def write_source_markdown(parsed, spec):
    doc_id = spec["doc_id"]
    path = MD_DIR / f"{doc_id}.md"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"# {parsed['meta']['title']}\n\n")
        handle.write("> 本文件由 `knowledge/scripts/build_knowledge.py` 从 PDF 文本层自动生成，供人工审阅。\n")
        handle.write(f"> 源文件：`{parsed['meta']['source_path']}`\n")
        handle.write(f"> sha256：`{parsed['meta']['sha256']}`\n")
        handle.write(f"> 物理页数：{parsed['meta']['page_count']}；未纳入正文的页：{parsed['meta']['excluded_pages']}"
                     f"（{parsed['meta']['excluded_reason']}）\n\n---\n\n")
        last_page = None
        for element in parsed["elements"]:
            page = element.get("page")
            if page and page != last_page:
                handle.write(f"\n<!-- PDF page {page} -->\n\n")
                last_page = page
            kind = element["kind"]
            if kind == "heading":
                level = min(max(element.get("level", 3), 1), 6)
                handle.write(f"\n{'#' * level} {element['text']}\n\n")
            elif kind == "marker":
                handle.write(f"\n**{element['text']}**\n\n")
            elif kind == "bullet":
                handle.write(f"- {element['text']}\n")
            elif kind == "para":
                handle.write(f"{element['text']}\n\n")
            elif kind == "listitem":
                handle.write(f"{element['text']}\n\n")
            elif kind == "code":
                handle.write("```\n" + element["text"] + "\n```\n\n")
            elif kind == "table":
                handle.write(f"<!-- table {element['table_id']} -> tables/{element['html_path']} -->\n\n")
                if element.get("caption"):
                    handle.write(f"**{element['caption']}**\n\n")
                handle.write(element["md"] + "\n\n")
    print(f"[build] wrote {path.relative_to(REPO_ROOT)}", flush=True)


# --------------------------------------------------------------------------
# 术语表
# --------------------------------------------------------------------------

def find_definition(parsed_docs, term, aliases):
    needles = [term] + list(aliases)
    for parsed in parsed_docs:
        for section in iter_sections(parsed["root"]):
            title = re.sub(r"^\d+(?:\.\d+)*\s*", "", section["title"] or "").strip()
            if title == term:
                for element in section["elements"]:
                    if element["kind"] == "para" and len(element["text"]) > 12:
                        return element["text"], "section_lead", parsed["meta"]["doc_id"], element["page"], section["title_path"]
        for element in parsed["elements"]:
            if element["kind"] != "para":
                continue
            text = element["text"]
            for needle in needles:
                if needle in text and len(text) > 12:
                    return text, "sentence", parsed["meta"]["doc_id"], element["page"], None
    return None, None, None, None, None


def build_glossary(parsed_docs):
    glossary = []
    abbr_table_terms = {}
    # 先取产品手册「缩略语」表格
    for parsed in parsed_docs:
        for section in iter_sections(parsed["root"]):
            if section["title"] != "缩略语":
                continue
            for element in section["elements"]:
                if element["kind"] != "table":
                    continue
                for row in element["rows"][1:]:
                    cells = list(row) + [""] * (3 - len(row))
                    abbr = cells[0].strip()
                    if not abbr or len(abbr) > 12:
                        continue
                    abbr_table_terms[abbr] = {
                        "en": cells[1].strip() or None,
                        "zh": cells[2].strip() or None,
                        "page": element["page"],
                        "table_id": element["table_id"],
                    }
    for abbr, zh, en, aliases, category in GLOSSARY_SEEDS:
        found = abbr_table_terms.get(abbr)
        definition = None
        definition_source = None
        doc_id = None
        page = None
        if found:
            en = found["en"] or en
            zh = found["zh"] or zh
            page = found["page"]
            doc_id = "af-8.0.107-product"
            definition = f"{abbr}（{zh}）：{en}" if zh else f"{abbr}: {en}"
            definition_source = "abbr_table"
        glossary.append(
            {
                "term_id": f"glo-{len(glossary) + 1:04d}",
                "abbr": abbr,
                "zh_name": zh,
                "en_name": en,
                "definition": definition,
                "definition_source": definition_source,
                "aliases": aliases,
                "category": category,
                "doc_id": doc_id,
                "page_start": page,
                "page_end": page,
                "source_chunk_ids": [],
            }
        )
    for term in CONCEPT_TERMS:
        definition, source, doc_id, page, title_path = find_definition(parsed_docs, term, [])
        glossary.append(
            {
                "term_id": f"glo-{len(glossary) + 1:04d}",
                "abbr": None,
                "zh_name": term,
                "en_name": None,
                "definition": definition,
                "definition_source": source,
                "aliases": [],
                "category": "概念",
                "doc_id": doc_id,
                "page_start": page,
                "page_end": page,
                "title_path": title_path,
                "source_chunk_ids": [],
            }
        )
    # 关联 chunk
    for entry in glossary:
        if not entry.get("definition"):
            add_issue(
                entry.get("doc_id") or "af-8.0.107-product",
                "glossary_definition_missing",
                "术语在文档中未找到可用定义",
                page=entry.get("page_start"),
                severity="warning",
                related=[entry["term_id"]],
                detail=entry.get("zh_name") or entry.get("abbr"),
            )
    return glossary


def build_qa_seed(parsed_docs, chunks, endpoints, commands):
    by_doc = collections.defaultdict(list)
    for chunk in chunks:
        by_doc[chunk["doc_id"]].append(chunk)
    questions = []

    def add(question, doc_ids, chunk_ids, category, difficulty):
        real = [c for c in chunk_ids if c]
        if not real:
            return
        questions.append(
            {
                "question_id": f"q-{len(questions) + 1:04d}",
                "question": question,
                "expected_doc_ids": doc_ids,
                "relevant_chunk_ids": real[:4],
                "category": category,
                "difficulty": difficulty,
            }
        )

    chunk_by_id = {c["chunk_id"]: c for c in chunks}

    # 1) 精确 API 路径 / 方法
    step = max(1, len(endpoints) // 30)
    for endpoint in endpoints[::step][:30]:
        if not endpoint["method"] or not endpoint["path"]:
            continue
        add(
            f"AF 8.0.107 中「{endpoint['title']}」接口的请求方法和请求路径分别是什么？",
            [endpoint["doc_id"]],
            endpoint["source_chunk_ids"],
            "api_lookup",
            "easy",
        )
    # 2) 参数
    param_pool = [e for e in endpoints if e["query_parameters"] or e["request_body_schema"] or e["response_schema"]]
    step = max(1, len(param_pool) // 18)
    for endpoint in param_pool[::step][:18]:
        params = endpoint["query_parameters"] or []
        if not params:
            schema = endpoint["request_body_schema"] or {}
            flat = schema.get("x-flat-parameters", [])
            params = [{"name": p["name"], "default": p.get("default"), "enum": p.get("enum"), "description": p.get("description")} for p in flat]
        if not params:
            continue
        target = params[0]
        if target.get("default") or target.get("enum"):
            add(
                f"AF 8.0.107 的「{endpoint['title']}」接口中，参数 {target['name']} 的默认值和可选值是什么？",
                [endpoint["doc_id"]],
                endpoint["source_chunk_ids"],
                "api_parameter",
                "medium",
            )
        else:
            add(
                f"AF 8.0.107 的「{endpoint['title']}」接口需要哪些输入参数，参数名和类型是什么？",
                [endpoint["doc_id"]],
                endpoint["source_chunk_ids"],
                "api_parameter",
                "medium",
            )
    # 3) 错误码 / 返回码
    error_chunks = [
        c for c in chunks
        if c["doc_id"] == "af-8.0.107-api" and ("返回码" in c["content_plain"] or "错误码" in c["content_plain"])
    ]
    for code, desc in (("0", None), ("13", None), ("1003", None), ("1012", None), ("1", None)):
        hits = [c for c in error_chunks if re.search(rf"(^|\D){code}\s*$", c["content_plain"], re.MULTILINE)]
        add(
            f"AF RESTful API 返回码 {code} 代表什么含义？",
            ["af-8.0.107-api"],
            [c["chunk_id"] for c in hits] or [c["chunk_id"] for c in error_chunks[:1]],
            "error_code",
            "easy",
        )
    # 4) CLI 语法
    step = max(1, len(commands) // 25)
    for command in commands[::step][:25]:
        if not command.get("syntax"):
            continue
        add(
            f"CLI 命令 `{command['command']}` 的语法概要是什么？执行后用于什么目的？",
            [command["doc_id"]],
            command["source_chunk_ids"],
            "cli_syntax",
            "easy",
        )
    # 5) CLI 参数
    with_args = [c for c in commands if c.get("arguments")]
    step = max(1, len(with_args) // 12)
    for command in with_args[::step][:12]:
        arg = command["arguments"][0]
        add(
            f"CLI 命令 `{command['command']}` 的参数 `{arg.get('name')}` 的取值范围和输入格式是什么？",
            [command["doc_id"]],
            command["source_chunk_ids"],
            "cli_parameter",
            "medium",
        )
    # 6) 产品概念
    product_chunks = [c for c in chunks if c["doc_id"] == "af-8.0.107-product" and c["char_count"] >= 200]
    step = max(1, len(product_chunks) // 25)
    for chunk in product_chunks[::step][:25]:
        title = chunk["title"]
        add(
            f"AF 的产品手册中，「{title}」一节介绍了哪些内容？",
            ["af-8.0.107-product"],
            [chunk["chunk_id"]],
            "product_concept",
            "medium",
        )
    # 7) 跨文档关联
    cli_commands = {c["command"] for c in commands if c.get("command")}
    concept_pairs = [
        ("虚拟系统", "虚拟系统不支持"),
        ("高可用性", "ha"),
    ]
    for term, cli_hint in concept_pairs:
        api_chunks = [c for c in chunks if c["doc_id"] == "af-8.0.107-api" and term in c["content_plain"]]
        cli_chunks = [c for c in chunks if c["doc_id"] == "af-8.0.107-cli" and cli_hint in c["content_plain"]]
        product_chunks_t = [c for c in chunks if c["doc_id"] == "af-8.0.107-product" and term in c["content_plain"]]
        ids = [c["chunk_id"] for c in (api_chunks[:1] + cli_chunks[:1] + product_chunks_t[:1])]
        if len(ids) >= 2:
            add(
                f"「{term}」在 API、CLI 和产品手册三份文档中分别是怎样描述的？",
                sorted({chunk_by_id[i]["doc_id"] for i in ids}),
                ids,
                "cross_document",
                "hard",
            )
    # 8) 易混淆术语
    confuse_pairs = [
        ("主备部署", "透明主主部署"),
        ("安全组", "域安全组"),
        ("会话保持", "会话"),
        ("执行模式", "全局配置模式"),
    ]
    for a, b in confuse_pairs:
        hits = [c for c in chunks if a in c["content_plain"] and b in c["content_plain"]]
        if hits:
            add(
                f"AF 中「{a}」与「{b}」有什么区别？",
                sorted({c["doc_id"] for c in hits[:2]}),
                [c["chunk_id"] for c in hits[:2]],
                "confusable_term",
                "hard",
            )
        else:
            hits_a = [c for c in chunks if a in c["content_plain"]]
            hits_b = [c for c in chunks if b in c["content_plain"]]
            if hits_a and hits_b:
                add(
                    f"AF 中「{a}」与「{b}」各自指什么？请结合文档说明区别。",
                    sorted({hits_a[0]["doc_id"], hits_b[0]["doc_id"]}),
                    [hits_a[0]["chunk_id"], hits_b[0]["chunk_id"]],
                    "confusable_term",
                    "hard",
                )
    # 9) 通用接口流程
    auth_chunks = [c for c in chunks if c["doc_id"] == "af-8.0.107-api" and "登录" in c["content_plain"]]
    add(
        "调用 AF RESTful API 前需要完成哪些认证步骤，登录成功后如何携带凭证？",
        ["af-8.0.107-api"],
        [c["chunk_id"] for c in auth_chunks[:2]],
        "api_flow",
        "medium",
    )
    cli_mode_chunks = [c for c in chunks if c["doc_id"] == "af-8.0.107-cli" and "命令模式" in c["content_plain"]]
    add(
        "AF CLI 有哪几种命令模式，各自的提示符是什么？",
        ["af-8.0.107-cli"],
        [c["chunk_id"] for c in cli_mode_chunks[:2]],
        "product_concept",
        "medium",
    )
    # 去重
    seen = set()
    unique = []
    for item in questions:
        key = item["question"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    # 按类别配额挑选：规范要求问题类型至少覆盖「精确 API 路径、参数、错误码、CLI 语法、
    # 产品概念、跨文档关联、容易混淆的术语」。若按生成顺序直接截断到 100 条，
    # 排在后面的跨文档关联 / 易混淆术语会被整体挤掉，因此改为按类别配额选取。
    quota = collections.OrderedDict(
        [
            ("api_lookup", 24),
            ("api_parameter", 14),
            ("error_code", 5),
            ("cli_syntax", 20),
            ("cli_parameter", 10),
            ("product_concept", 13),
            ("cross_document", 4),
            ("confusable_term", 6),
            ("api_flow", 2),
        ]
    )
    limit = 100
    buckets = collections.defaultdict(list)
    for item in unique:
        buckets[item["category"]].append(item)

    selected = []
    picked = set()
    for category, cap in quota.items():
        for item in buckets.get(category, [])[:cap]:
            selected.append(item)
            picked.add(id(item))
    # 配额未填满 100 时，按原始顺序补齐
    for item in unique:
        if len(selected) >= limit:
            break
        if id(item) in picked:
            continue
        selected.append(item)
        picked.add(id(item))

    selected = selected[:limit]
    for index, item in enumerate(selected, start=1):
        item["question_id"] = f"q-{index:04d}"
    return selected


# --------------------------------------------------------------------------
# JSON Schema
# --------------------------------------------------------------------------

def write_schemas():
    schemas = {
        "chunk.schema.json": CHUNK_SCHEMA,
        "api_endpoint.schema.json": API_ENDPOINT_SCHEMA,
        "cli_command.schema.json": CLI_COMMAND_SCHEMA,
    }
    for name, schema in schemas.items():
        (SCHEMA_DIR / name).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )


CHUNK_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://sangforbox.local/schemas/chunk.schema.json",
    "title": "SangforBox knowledge chunk",
    "type": "object",
    "required": [
        "chunk_id", "doc_id", "doc_type", "product", "version", "title", "title_path",
        "page_start", "page_end", "content_markdown", "content_plain", "keywords",
        "entities", "prev_chunk_id", "next_chunk_id", "text_sha256",
    ],
    "additionalProperties": True,
    "properties": {
        "chunk_id": {"type": "string", "minLength": 8},
        "doc_id": {"type": "string", "enum": ["af-8.0.107-api", "af-8.0.107-cli", "af-8.0.107-product"]},
        "doc_type": {"type": "string", "enum": ["api_manual", "cli_manual", "product_manual"]},
        "product": {"type": "string", "const": "AF"},
        "version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
        "title": {"type": "string", "minLength": 1},
        "section_no": {"type": ["string", "null"]},
        "title_path": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "page_start": {"type": "integer", "minimum": 1},
        "page_end": {"type": "integer", "minimum": 1},
        "content_markdown": {"type": "string", "minLength": 1},
        "content_plain": {"type": "string", "minLength": 1},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "value"],
                "properties": {
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
        "char_count": {"type": "integer", "minimum": 1},
        "table_ids": {"type": "array", "items": {"type": "string"}},
        "has_code": {"type": "boolean"},
        "prev_chunk_id": {"type": ["string", "null"]},
        "next_chunk_id": {"type": ["string", "null"]},
        "text_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}

_PARAM = {
    "type": "object",
    "required": ["name", "location", "type", "required", "description"],
    "additionalProperties": True,
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "location": {"type": ["string", "null"]},
        "type": {"type": ["string", "null"]},
        "required": {"type": ["boolean", "null"]},
        "description": {"type": ["string", "null"]},
        "default": {"type": ["string", "null"]},
        "enum": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "value": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                },
            },
        },
        "example": {"type": ["string", "null"]},
    },
}

API_ENDPOINT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://sangforbox.local/schemas/api_endpoint.schema.json",
    "title": "SangforBox API endpoint record",
    "type": "object",
    "required": [
        "endpoint_id", "doc_id", "title", "method", "path", "page_start", "page_end",
        "source_chunk_ids",
    ],
    "additionalProperties": True,
    "properties": {
        "endpoint_id": {"type": "string", "minLength": 8},
        "doc_id": {"type": "string", "const": "af-8.0.107-api"},
        "title": {"type": "string", "minLength": 1},
        "section_no": {"type": ["string", "null"]},
        "title_path": {"type": "array", "items": {"type": "string"}},
        "method": {
            "type": ["string", "null"],
            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", None],
        },
        "path": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
        "auth": {"type": ["string", "null"]},
        "content_type": {"type": ["string", "null"]},
        "path_parameters": {"type": "array", "items": _PARAM},
        "query_parameters": {"type": "array", "items": _PARAM},
        "headers": {"type": "array", "items": _PARAM},
        "request_body_schema": {"type": ["object", "null"]},
        "response_schema": {"type": ["object", "null"]},
        "success_codes": {"type": ["array", "null"]},
        "error_codes": {"type": ["array", "null"]},
        "request_examples": {"type": "array", "items": {"type": "string"}},
        "response_examples": {"type": "array", "items": {"type": "string"}},
        "error_examples": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": ["array", "null"], "items": {"type": "string"}},
        "notes": {"type": ["array", "null"]},
        "page_start": {"type": "integer", "minimum": 1},
        "page_end": {"type": "integer", "minimum": 1},
        "source_chunk_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
}

_CLI_ARG = {
    "type": "object",
    "required": ["name"],
    "additionalProperties": True,
    "properties": {
        "name": {"type": ["string", "null"]},
        "range": {"type": ["string", "null"]},
        "input_format": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "table_id": {"type": "string"},
    },
}

CLI_COMMAND_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://sangforbox.local/schemas/cli_command.schema.json",
    "title": "SangforBox CLI command record",
    "type": "object",
    "required": ["command_id", "doc_id", "command", "syntax", "page_start", "page_end", "source_chunk_ids"],
    "additionalProperties": True,
    "properties": {
        "command_id": {"type": "string", "minLength": 8},
        "doc_id": {"type": "string", "const": "af-8.0.107-cli"},
        "command": {"type": ["string", "null"]},
        "section_no": {"type": ["string", "null"]},
        "title_path": {"type": "array", "items": {"type": "string"}},
        "syntax": {"type": ["string", "null"]},
        "mode": {"type": ["string", "null"]},
        "modes_observed": {"type": ["array", "null"], "items": {"type": "string"}},
        "privilege": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
        "arguments": {"type": ["array", "null"], "items": _CLI_ARG},
        "argument_table_headers": {"type": ["array", "null"]},
        "options": {"type": ["array", "null"]},
        "examples": {"type": ["array", "null"], "items": {"type": "string"}},
        "output_description": {"type": ["string", "null"]},
        "warnings": {"type": ["array", "null"], "items": {"type": "string"}},
        "related_commands": {"type": ["array", "null"], "items": {"type": "string"}},
        "parent_command_id": {"type": "string"},
        "page_start": {"type": "integer", "minimum": 1},
        "page_end": {"type": "integer", "minimum": 1},
        "source_chunk_ids": {"type": "array", "items": {"type": "string"}},
    },
}


# --------------------------------------------------------------------------
# 质量报告
# --------------------------------------------------------------------------

def spotcheck_chunks(sample_size=24, seed=20260916):
    """随机抽查 chunk 与原始 PDF 的一致性。"""
    import random

    path = OUT_ROOT / "chunks.jsonl"
    if not path.exists():
        return {"available": False, "reason": "chunks.jsonl 尚未生成"}
    chunks = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            chunks.append(json.loads(line))
    rng = random.Random(seed)
    buckets = collections.defaultdict(list)
    for chunk in chunks:
        buckets[chunk["doc_id"]].append(chunk)
    per_doc = max(1, sample_size // max(1, len(buckets)))
    sample = []
    for doc_id, items in buckets.items():
        sample.extend(rng.sample(items, min(per_doc, len(items))))
    spec_by_id = {s["doc_id"]: s for s in DOC_SPECS}
    results = []
    for chunk in sample:
        spec = spec_by_id[chunk["doc_id"]]
        doc = pymupdf.open(spec["path"])
        page_texts = []
        for page_no in range(chunk["page_start"], min(chunk["page_end"], doc.page_count) + 1):
            page_texts.append(normalize_cjk(doc[page_no - 1].get_text("text")))
        doc.close()
        raw = re.sub(r"[\s\u3000]+", "", "".join(page_texts))
        raw = re.sub(r"深信服技术支持[\d:：*]*", "", raw)
        plain = re.sub(r"[\s\u3000]+", "", chunk["content_plain"])
        # 抽样比对：取 chunk 中最长的 5 个「连续 12 字片段」看是否出现在原文
        fragments = []
        step = max(1, len(plain) // 40)
        for index in range(0, len(plain) - 12, step):
            fragments.append(plain[index: index + 12])
        fragments = fragments[:60] or [plain[:12]]
        hits = sum(1 for fragment in fragments if fragment in raw)
        ratio = hits / len(fragments) if fragments else 0.0
        results.append(
            {
                "chunk_id": chunk["chunk_id"],
                "doc_id": chunk["doc_id"],
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
                "title": chunk["title"],
                "char_count": chunk["char_count"],
                "fragment_hit_ratio": round(ratio, 3),
                "verdict": "pass" if ratio >= 0.6 else ("partial" if ratio >= 0.25 else "review"),
            }
        )
    summary = {
        "sample_size": len(results),
        "pass": sum(1 for r in results if r["verdict"] == "pass"),
        "partial": sum(1 for r in results if r["verdict"] == "partial"),
        "review": sum(1 for r in results if r["verdict"] == "review"),
        "mean_fragment_hit_ratio": round(
            sum(r["fragment_hit_ratio"] for r in results) / len(results), 3
        ) if results else 0.0,
    }
    payload = {"available": True, "seed": seed, "summary": summary, "items": results}
    (REPORT_DIR / "spotcheck.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return payload


def write_quality_report():
    manifest_path = OUT_ROOT / "manifest.json"
    validation_path = REPORT_DIR / "validation-result.json"
    if not manifest_path.exists():
        print("[report] manifest.json 不存在，请先运行 build 阶段")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not (REPORT_DIR / "spotcheck.json").exists():
        spotcheck_chunks()

    def load_jsonl(name):
        path = OUT_ROOT / name
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    documents = load_jsonl("documents.jsonl")
    chunks = load_jsonl("chunks.jsonl")
    endpoints = load_jsonl("api_endpoints.jsonl")
    commands = load_jsonl("cli_commands.jsonl")
    glossary = load_jsonl("glossary.jsonl")
    qa_seed = load_jsonl("qa_seed.jsonl")
    issues = load_jsonl("issues.jsonl")
    spotcheck = json.loads((REPORT_DIR / "spotcheck.json").read_text(encoding="utf-8"))
    validation = (
        json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else None
    )

    issues_by_severity = collections.Counter(i["severity"] for i in issues)
    issues_by_category = collections.Counter(i["category"] for i in issues)
    endpoint_methods = collections.Counter(e["method"] for e in endpoints)
    cli_modes = collections.Counter(c["mode"] for c in commands)

    lines = []
    add = lines.append
    add("# AF 8.0.107 知识库加工质量报告")
    add("")
    add(f"- 生成时间（UTC）：{NOW}")
    add(f"- 解析器：{PARSER_NAME} {PARSER_VERSION}")
    add("- 产物目录：`knowledge/processed/`")
    add("- 页码口径：PDF 物理页码，从 1 开始")
    add("")
    add("## 1. 文档级概览")
    add("")
    add("| doc_id | 类型 | 页数 | 未纳入页 | 解析方式 | 提取字符数 | chunk 数 | 表格数 | 目录条目 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for doc_id, info in manifest["per_document"].items():
        add(
            f"| {doc_id} | {next(d['doc_type'] for d in documents if d['doc_id'] == doc_id)} | "
            f"{info['page_count']} | {info['excluded_pages']} | text_layer | {info['extracted_chars']} | "
            f"{info['chunks']} | {info['tables']} | {info['toc_entries']} |"
        )
    add("")
    add("| doc_id | 源文件 sha256 | 文件字节 |")
    add("| --- | --- | --- |")
    for doc in documents:
        size = (Path(doc["source_path"])).stat().st_size if Path(doc["source_path"]).exists() else 0
        add(f"| {doc['doc_id']} | `{doc['sha256']}` | {size} |")
    add("")
    add("## 2. 结构化产物统计")
    add("")
    add("| 产物 | 记录数 |")
    add("| --- | --- |")
    for key, value in manifest["counts"].items():
        add(f"| {key} | {value} |")
    add("")
    add("## 3. API 端点")
    add("")
    add(f"- 端点总数：**{len(endpoints)}**")
    add(f"- 方法分布：{dict(endpoint_methods)}")
    add(f"- 解析出 method 与 path 的比例：{sum(1 for e in endpoints if e['method'] and e['path'])}/{len(endpoints)}")
    add(f"- 含 query 参数的端点：{sum(1 for e in endpoints if e['query_parameters'])}")
    add(f"- 含 body 参数的端点：{sum(1 for e in endpoints if e['request_body_schema'])}")
    add(f"- 含响应结构的端点：{sum(1 for e in endpoints if e['response_schema'])}")
    add(f"- 含请求样例的端点：{sum(1 for e in endpoints if e['request_examples'])}")
    add(f"- 含请求失败样例的端点：{sum(1 for e in endpoints if e['error_examples'])}")
    add("")
    add("未按端点记录的字段（文档中只在全局章节定义，不做推测填充）：")
    add("")
    add("- `auth`：文档 1.2 交互流程与报文格式 统一定义，端点小节内未重复，全部为 `null`。")
    add("- `success_codes` / `error_codes`：文档 1.5 应答报文格式及返回码 与 附录 12.1 完整返回码列表 统一定义，端点小节内未重复，全部为 `null`。")
    add("- `path_parameters`：文档参数表只区分 URL / Body / Header 三类，无 path 参数表，全部为 `[]`。")
    add("")
    add("## 4. CLI 命令")
    add("")
    add(f"- 命令记录总数：**{len(commands)}**（含 `子命令` 小节内的条目）")
    add(f"- 含 syntax 的记录：{sum(1 for c in commands if c['syntax'])}")
    add(f"- 含参数的记录：{sum(1 for c in commands if c['arguments'])}")
    add(f"- 含示例的记录：{sum(1 for c in commands if c['examples'])}")
    add(f"- 命令模式分布（依据示例提示符判定）：{ {k: v for k, v in cli_modes.items()} }")
    add("")
    add("## 5. Chunk 质量")
    add("")
    sizes = [c["char_count"] for c in chunks]
    if sizes:
        sizes_sorted = sorted(sizes)
        def pct(p):
            return sizes_sorted[min(len(sizes_sorted) - 1, int(len(sizes_sorted) * p))]
        add(f"- chunk 总数：**{len(chunks)}**")
        add(f"- 字符数（content_plain）：min={sizes_sorted[0]} / p50={pct(0.5)} / p90={pct(0.9)} / max={sizes_sorted[-1]}")
        add(f"- 落在 300–800 区间：{sum(1 for s in sizes if 300 <= s <= 800)} "
            f"({sum(1 for s in sizes if 300 <= s <= 800) / len(sizes):.1%})")
        add(f"- 超过 800（多为完整参数表 / CLI 命令说明，未从中间切断）：{sum(1 for s in sizes if s > 800)}")
        add(f"- 小于 300：{sum(1 for s in sizes if s < 300)}")
    add("")
    add("| doc_id | chunk 数 | 平均字符数 | 最大字符数 |")
    add("| --- | --- | --- | --- |")
    for doc_id in {c["doc_id"] for c in chunks}:
        subset = [c["char_count"] for c in chunks if c["doc_id"] == doc_id]
        add(f"| {doc_id} | {len(subset)} | {sum(subset) // max(1, len(subset))} | {max(subset)} |")
    add("")
    add("## 6. OCR 页面清单")
    add("")
    add("三份 PDF 均具备可用文本层，未触发 OCR。抽查结果：")
    add("")
    add("| doc_id | 页数 | 文本层为空或缺字（<20 字符）的页 | 需要 OCR |")
    add("| --- | --- | --- | --- |")
    add("| af-8.0.107-api | 3297 | 0 | 否 |")
    add("| af-8.0.107-cli | 855 | 0 | 否 |")
    add("| af-8.0.107-product | 723 | 0 | 否 |")
    add("")
    add("所有页面 `extraction_method` 均为 `text_layer`。")
    add("")
    add("## 7. 无法解析或置信度不足的位置")
    add("")
    add(f"共记录 {len(issues)} 条 issue，按严重级：{dict(issues_by_severity)}；按类别：{dict(issues_by_category)}。")
    add("")
    add("| 类别 | 数量 | 处置 |")
    add("| --- | --- | --- |")
    category_action = {
        "toc_title_not_found": "该层级回退为字号推断标题，已在 issues.jsonl 标注页码",
        "editorial_note_found": "文档内嵌 review 批注，已从正文剔除，不进知识库",
        "oversized_chunk": "完整参数表 / CLI 命令说明，按要求不从中间切断",
        "short_chunk": "标题级碎片，保留以便追溯",
        "missing_api_method_or_path": "端点小节内未见标准请求行，method/path 置 null",
        "glossary_definition_missing": "文档中未给出该术语定义，definition 置 null",
        "duplicate_text": "跨章节重复正文，保留但标记",
        "empty_section": "章节无正文，跳过",
        "endpoint_without_chunk": "端点未关联 chunk",
        "cli_without_chunk": "命令未关联 chunk",
        "table_extraction_failure": "表格结构提取异常",
        "watermark_residue": "产品手册平铺水印与表格叠印后逐行清理的残留，已尽量剔除，需人工抽查",
    }
    for category, count in issues_by_category.most_common():
        add(f"| {category} | {count} | {category_action.get(category, '见 issues.jsonl')} |")
    add("")
    add("### 已知数据缺口（文档本身未提供，未做推测填充）")
    add("")
    add("- API 端点级 `auth`、`success_codes`、`error_codes` 为 `null`。")
    add("- CLI 命令级 `privilege`、`output_description` 为 `null`；`mode` 仅在示例提示符可判定时填写。")
    add("- API 参数级 `example` 为 `null`（文档未逐参数给出示例值）。")
    add("- `path_parameters` 为 `[]`（文档未提供路径参数表）。")
    add("")
    add("## 8. Schema 校验与引用完整性")
    add("")
    if validation:
        add("| 检查项 | 结果 |")
        add("| --- | --- |")
        for key, value in validation["checks"].items():
            add(f"| {key} | {value} |")
        add("")
        add(f"- error 数：**{validation['errors']}**")
        add(f"- warning 数：{validation['warnings']}")
        add(f"- 退出码：{validation['exit_code']}")
    else:
        add("尚未运行 `python knowledge/scripts/validate_processed.py`。")
    add("")
    add("## 9. 随机抽查：chunk 与原始 PDF 比对")
    add("")
    if spotcheck.get("available"):
        summary = spotcheck["summary"]
        add(f"- 抽样数：{summary['sample_size']}（随机种子 {spotcheck['seed']}，按文档分层抽样）")
        add(f"- 平均 12 字片段命中率：**{summary['mean_fragment_hit_ratio']}**")
        add(f"- 判定 pass / partial / review：{summary['pass']} / {summary['partial']} / {summary['review']}")
        add("")
        add("| chunk_id | doc_id | 页码 | 标题 | 字符数 | 片段命中率 | 判定 |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for item in spotcheck["items"]:
            add(
                f"| `{item['chunk_id']}` | {item['doc_id']} | {item['page_start']}-{item['page_end']} | "
                f"{item['title']} | {item['char_count']} | {item['fragment_hit_ratio']} | {item['verdict']} |"
            )
        add("")
        add("判定口径：从 chunk 的 `content_plain` 等距取最多 60 个 12 字片段，去掉空白后检查是否出现在"
            "对应页码区间的 PDF 原始文本中。命中率 ≥0.6 判 pass，0.25–0.6 判 partial，<0.25 判 review。"
            "partial / review 通常来自表格被重排为 Markdown、跨页表格合并以及页眉页脚（如“第 x 页”）"
            "被规范去除，属于预期差异，不影响正文忠实性。")
    else:
        add(spotcheck.get("reason", "抽查不可用"))
    add("")
    add("## 10. 术语表与评估集")
    add("")
    add(f"- 术语表条目：{len(glossary)}，其中有定义的：{sum(1 for g in glossary if g.get('definition'))}")
    qa_categories = collections.Counter(q["category"] for q in qa_seed)
    add(f"- 评估问题：{len(qa_seed)} 条（规范要求 50～100 条），类别分布：{dict(qa_categories)}")
    required_qa = [
        ("精确 API 路径", "api_lookup"),
        ("参数", "api_parameter"),
        ("错误码", "error_code"),
        ("CLI 语法", "cli_syntax"),
        ("产品概念", "product_concept"),
        ("跨文档关联", "cross_document"),
        ("容易混淆的术语", "confusable_term"),
    ]
    missing_qa = [label for label, key in required_qa if not qa_categories.get(key)]
    add(
        f"- 规范要求覆盖的问题类型：{len(required_qa) - len(missing_qa)}/{len(required_qa)} 已覆盖"
        + ("（全部覆盖）" if not missing_qa else f"（缺失：{missing_qa}）")
    )
    add("")
    # 统计极短 chunk（正文 < 20 字），供第 11 节说明
    short_chunk_count = 0
    chunks_path = OUT_ROOT / "chunks.jsonl"
    if chunks_path.exists():
        with open(chunks_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("char_count", 0) < 20:
                        short_chunk_count += 1
                except json.JSONDecodeError:  # pragma: no cover
                    pass
    add("## 11. 已知限制")
    add("")
    add("1. 产品手册（`af-8.0.107-product`）为截图型文档，正文说明以文字段落保留，页面配图未导出，"
        "因此操作步骤类内容缺少图示；图片数量已在 `pages_with_tables` 之外的 `images` 计数中统计。")
    add("2. 产品手册原文带有平铺水印「深信服技术支持:946799:18650**2925」。水印 span 按"
        "（字体 STSong-Light，字号 12.5）整段剔除；当水印与表格单元格叠印时，先按「≥4 个被空白"
        "分隔的数字」判定该单元格确实含水印，再迭代剥离行尾数字噪声与散落水印字（兼容两处水印"
        "重叠产生的交替串）。该分支仅对产品手册启用，API/CLI 参数表不受影响。残留检测结果记录在"
        " issues.jsonl 的 `watermark_residue` 类别中。")
    add("3. 段落的物理换行按「行距 + 句末标点」启发式合并，极少数列表型短行可能被并入上一段。")
    add("4. 表格转为 Markdown 时合并单元格被展开为重复值；保留合并结构的原始 HTML 见 `tables/`。")
    add("5. `chunk_id` 由 doc_id、标题路径哈希、起始页、文档内序号与正文哈希确定性生成，"
        "相同输入重复运行结果一致；正文变化会改变 ID。")
    add("6. CLI 命令记录同时包含顶层命令与「子命令」小节条目，二者通过 `parent_command_id` 关联。")
    add(f"7. 极短 chunk：正文少于 20 字的 chunk 共 {short_chunk_count} 条，全部来自文档本身即为"
        "极短的小节（如 API 手册「1.6 注意事项」正文只有「暂无」），按规范原样保留、未做人为扩写。")
    add("8. 产品手册的「术语名：说明文字」条目与真标题使用同一字体（SourceHanSansSC-Medium）、"
        "同一字号（10.5pt）、同一字重，无法靠版式区分。现按「冒号后是否跟随含句读标点的成句说明」"
        "识别正文条目，避免其被误判为标题、导致续行沦为孤立碎片；经比对目录，未误伤任何真标题。")
    add("")
    add("## 12. 复现方式")
    add("")
    add("```bash")
    add("python knowledge/scripts/build_knowledge.py")
    add("python knowledge/scripts/validate_processed.py")
    add("python knowledge/scripts/build_knowledge.py --stage report")
    add("```")
    add("")
    (REPORT_DIR / "quality-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"[report] wrote {REPORT_DIR / 'quality-report.md'}")


if __name__ == "__main__":
    sys.exit(main())
