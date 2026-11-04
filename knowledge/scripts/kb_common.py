"""SangforBox 知识库加工流水线 —— 通用工具层。

本模块只包含与具体文档无关的解析原语：文本归一化、水印清理、
页码/哈希工具、Markdown 渲染、关键词与实体抽取。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# --------------------------------------------------------------------------
# 文本归一化
# --------------------------------------------------------------------------

# 仅对「康熙部首 / CJK 部首补充 / CJK 兼容表意文字」做还原。
# 这些码位是 PDF 生成器把汉字写成异体码位的产物（如 ⻚→页、⼿→手），
# 不做全量 NFKC，以免把全角标点、全角字母改写成半角而改变原文语义。
_RADICAL_BLOCKS = ((0x2E80, 0x2FDF), (0xF900, 0xFAFF))

# NFKC 无法覆盖的码位（CJK 部首补充区）以及 NFKC 会还原成繁体的情况，显式指定。
_RADICAL_OVERRIDES = {
    "\u2eda": "页",  # ⻚ CJK RADICAL C-SIMPLIFIED LEAF
    "\u2edb": "风",  # ⻛ CJK RADICAL C-SIMPLIFIED WIND
    "\u2ee5": "鱼",  # ⻥ CJK RADICAL C-SIMPLIFIED FISH
    "\u2ec6": "角",  # ⻆ CJK RADICAL SIMPLIFIED HORN
    "\u2ed3": "长",  # ⻓ CJK RADICAL C-SIMPLIFIED LONG
    "\u2ec5": "见",  # ⻅ CJK RADICAL C-SIMPLIFIED SEE
    "\u2ecb": "车",  # ⻋ CJK RADICAL C-SIMPLIFIED CART
    "\u2f3e": "户",  # ⼾ KANGXI RADICAL DOOR（NFKC 会得到繁体「戶」）
}


def normalize_cjk(text: str) -> str:
    """把康熙部首 / CJK 部首 / 兼容表意文字还原为标准简体汉字，其余字符原样保留。"""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in _RADICAL_OVERRIDES:
            out.append(_RADICAL_OVERRIDES[ch])
            continue
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in _RADICAL_BLOCKS):
            fixed = unicodedata.normalize("NFKC", ch)
            out.append(_RADICAL_OVERRIDES.get(fixed, fixed))
        else:
            out.append(ch)
    return "".join(out)


_WS_RE = re.compile(r"[\u00a0\u3000\ufeff\u200b]+")


def squeeze_spaces(text: str) -> str:
    """压缩空白：去掉零宽字符、不换行空格，折叠连续空格。"""
    if not text:
        return ""
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# --------------------------------------------------------------------------
# 水印 / 页眉页脚清理
# --------------------------------------------------------------------------

# 产品手册的水印为平铺字符串：深信服技术支持:946799:18650**2925
_WM_FULL = re.compile(r"深信服技术支持\s*[:：]?\s*[\d\s:：*]*")
_WM_CHARS = set("深信服技术支持")
# 「数字 + 冒号 + 星号」拼成的噪声串
_WM_SOUP = re.compile(r"[\d\s:：*．]{6,}")
_WM_SOUP_TAIL = re.compile(r"\s+[\d\s:：*]{4,}\s*$")
_WM_SOUP_HEAD = re.compile(r"^[\d\s:：*]{4,}\s+")

_WM_CHAR_CLASS = "".join(sorted(_WM_CHARS))
# 水印特征标记：≥4 个数字被空白分隔（平铺水印被逐字拆开后的形态），
# 或「冒号 + 连续数字」。只对命中该标记的单元格启用激进剥离，避免误伤正文。
_WM_MARK = re.compile(r"\d(?:[\s]*\d){3,}|[:：]\s*\d(?:\s*\d){3,}")
# 行尾从最后一个数字起、全由噪声字符构成的尾段（比 _WM_SOUP_TAIL 更强，不要求前导空白）
_WM_TAIL_NOISE = re.compile(r"[\s:：*．]*\d(?:[\d\s:：*．]){3,}\s*$")
# 行尾连续的水印散字（「深信服技术支持」被平铺水印带入的孤立字，如「… 持」「术 支 持」）
_WM_TAIL_CHARS = re.compile(r"(?:\s*[" + _WM_CHAR_CLASS + r"]){1,3}\s*$")
# 整行仅由水印散字与空白组成
_WM_CHARS_ONLY = re.compile(r"[\s" + _WM_CHAR_CLASS + r"]+")

# 页脚特征（产品手册）
_FOOTER_HINT = re.compile(
    r"^(第\s*\d+\s*[页⻚]|版权所有[:：]|密级[:：]|[下下⼀一]?[一]?代防[火⽕]墙\s*AF_)"
)

# 产品手册内嵌的编辑批注（非产品内容）
_EDITOR_NOTE = re.compile(r"^\[[A-Za-z]{1,6}\d+\]")


def strip_watermark_text(text: str) -> str:
    """从任意文本片段中剔除产品手册水印残留。"""
    if not text:
        return ""
    text = _WM_FULL.sub("", text)
    text = _WM_SOUP.sub(" ", text)
    return text


def clean_table_cell(text: str, is_product: bool = False) -> str:
    """清理表格单元格：先归一化汉字，再逐行剔除水印碎片。

    产品手册的平铺水印 ``深信服技术支持:946799:18650**2925`` 会与单元格文字叠印，
    被逐字打散成「功能 持 :9 4 6 7 9 9 :1 8 6 5 0 * * 2 9 2 5」这类形态；同一单元格
    偶发两处水印重叠时还会出现「术 支 持 :9 4 6 7 9 9 9 :1 :1 8 8 6 6 …」的交替串。

    ``is_product`` 仅产品手册为 True（水印只存在于该手册）；为 False 时只做基础清理，
    保证 API/CLI 参数表（含大量数字的取值范围、默认值）不被误动。

    产品手册分支的策略：
    1. 仅当单元格命中 ``_WM_MARK``（≥4 个被空白分隔的数字）时才启用激进剥离；
    2. 激进分支迭代剥离「行尾数字噪声 → 行首噪声 → 行尾水印散字」，反复至稳定；
    3. 剥离后若整行只剩水印散字（「术 支 持」）或纯噪声，则丢弃该行。
    """
    if not text:
        return ""
    text = normalize_cjk(text)
    text = _WM_FULL.sub("", text)
    aggressive = bool(is_product and _WM_MARK.search(text))
    kept = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # 纯数字/冒号/星号噪声
        if re.fullmatch(r"[\d\s:：*．.、]+", line):
            continue
        if aggressive:
            for _ in range(4):
                before = line
                line = _WM_TAIL_NOISE.sub("", line)
                line = _WM_SOUP_HEAD.sub("", line)
                line = _WM_TAIL_CHARS.sub("", line)
                line = line.strip()
                if line == before:
                    break
            if not line:
                continue
            # 剥离后整行只剩水印散字（如「术 支 持」）
            if _WM_CHARS_ONLY.fullmatch(line):
                continue
        else:
            line = _WM_SOUP_TAIL.sub("", line)
            line = _WM_SOUP_HEAD.sub("", line)
            line = line.strip()
            if not line:
                continue
            # 孤立的单个水印字
            if len(line) == 1 and line in _WM_CHARS:
                continue
            # 「一个水印字 + 数字噪声」
            if re.fullmatch(r"[\u4e00-\u9fff]?\s*[\d\s:：*]+", line):
                continue
            # 行尾孤立的水印字（前置空白 + 单字，避免误伤「支持」这类正常词）
            line = re.sub(r"\s+[" + _WM_CHAR_CLASS + r"]\s*$", "", line).strip()
            if not line:
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def looks_like_watermark_residue(text: str) -> bool:
    """判断清理后的文本是否仍疑似水印残留（用于产出 issues）。"""
    if not text:
        return False
    if "深信服技术支持" in text:
        return True
    if re.search(r"\d(?:\s+\d){4,}", text):
        return True
    if re.search(r"[:：]\s*\d(?:\s*\d){3,}", text):
        return True
    return False


# --------------------------------------------------------------------------
# 哈希与 ID
# --------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, *parts) -> str:
    """由若干片段确定性生成稳定 ID。"""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


# --------------------------------------------------------------------------
# 文本统计与形态判断
# --------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ASCII_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_API_PATH_RE = re.compile(r"/[A-Za-z0-9_@{}$./\-]*[A-Za-z0-9_}]")
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_METHOD_LINE_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)\s*$")
_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
_ERROR_CODE_RE = re.compile(r"\b(?:code|返回码|错误码)\s*[:：]?\s*(-?\d+)\b")
_CLI_PROMPT_RE = re.compile(r"^hostname[a-zA-Z0-9\-]*(?:\([\w\-]+\))?\s*[#>]")


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_CJK_RE.findall(text)) / len(text)


def is_terminal(text: str) -> bool:
    """行尾是否为句末标点（用于段落合并判断）。"""
    if not text:
        return False
    return text.rstrip()[-1] in "。！？；：!?;:）)】》」』\"'、,，"


def looks_like_method_line(text: str) -> bool:
    return bool(_METHOD_LINE_RE.match(text.strip()))


def parse_method_line(text: str):
    match = _METHOD_LINE_RE.match(text.strip())
    if not match:
        return None, None
    return match.group(1), match.group(2)


# --------------------------------------------------------------------------
# Markdown / 纯文本渲染
# --------------------------------------------------------------------------

_MD_LINK_SAFE = re.compile(r"([\\`*_{}\[\]()#+\-.!|])")


def md_escape_inline(text: str) -> str:
    """表格单元格内联转义：仅处理会破坏表格结构的竖线与换行。"""
    text = text.replace("|", "\\|")
    return text.replace("\n", "<br>")


def table_to_markdown(rows, header=None) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    head = header if header else norm[0]
    body = norm if header is None else norm
    out = ["| " + " | ".join(md_escape_inline(str(c or "")) for c in head) + " |"]
    out.append("| " + " | ".join(["---"] * width) + " |")
    for row in body:
        out.append("| " + " | ".join(md_escape_inline(str(c or "")) for c in row) + " |")
    return "\n".join(out)


def markdown_to_plain(markdown: str) -> str:
    """把 content_markdown 转成用于 embedding 的纯文本。"""
    if not markdown:
        return ""
    lines = []
    in_fence = False
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(line.rstrip())
            continue
        stripped = re.sub(r"^#{1,6}\s*", "", stripped)
        stripped = re.sub(r"^[-*+]\s+", "", stripped)
        stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
        stripped = re.sub(r"^\|", "", stripped)
        stripped = re.sub(r"\|$", "", stripped)
        stripped = stripped.replace("---", " ")
        stripped = stripped.replace("\\|", "|")
        stripped = re.sub(r"[ \t]{2,}", " ", stripped)
        lines.append(stripped.strip())
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# 关键词与实体
# --------------------------------------------------------------------------

_STOPWORDS = {
    "如下", "所示", "以上", "以下", "说明", "参数", "类型", "描述", "名称",
    "是否必选", "默认值", "枚举值", "格式", "样例", "示例", "无", "是", "否",
    "本文档", "可以", "进行", "支持", "使用", "通过", "以及", "并且", "例如",
    "如果", "需要", "用户", "设备", "功能", "内容", "信息", "相关", "对应",
    "注意", "注意:", "说明:", "前提", "第", "页", "命令", "配置", "系统",
}


def extract_ascii_tokens(text: str, limit: int = 40):
    tokens = []
    for match in _ASCII_IDENT_RE.finditer(text or ""):
        token = match.group(0)
        if len(token) < 3:
            continue
        tokens.append(token)
    seen = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return seen[:limit]


def build_keywords(text, vocabulary, limit: int = 12):
    """关键词 = 命中的领域词表（按长度降序）+ ASCII 标识符。

    vocabulary 可以是术语集合，也可以是预编译的正则（更快）。
    """
    keywords = []
    if vocabulary is not None:
        hits = []
        if hasattr(vocabulary, "finditer"):
            hits = [m.group(0) for m in vocabulary.finditer(text)]
        else:
            hits = [term for term in vocabulary if len(term) >= 2 and term in text]
        hits = sorted(set(hits), key=lambda t: (-len(t), t))
        keywords.extend(hits[: limit // 2])
    for token in extract_ascii_tokens(text, limit=limit):
        if token not in keywords:
            keywords.append(token)
    result = []
    for item in keywords:
        if item not in _STOPWORDS and item not in result:
            result.append(item)
    return result[:limit]
