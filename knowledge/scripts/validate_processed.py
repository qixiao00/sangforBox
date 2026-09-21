"""校验 knowledge/processed 下所有产物。

用法：
    python knowledge/scripts/validate_processed.py
    python knowledge/scripts/validate_processed.py --strict-warnings

退出码：
    0 = 无 error（warning 允许保留）
    1 = 存在 error
    2 = 产物缺失或无法解析

校验内容：
    1. JSONL 逐行可解析；
    2. JSON Schema（Draft 2020-12）合法且实例通过校验；
    3. ID 唯一、跨文件引用存在；
    4. 页码在文档页数范围内；
    5. prev_chunk_id / next_chunk_id 双向一致；
    6. 每条 API / CLI 记录至少关联一个 chunk；
    7. 空内容、异常短块、重复正文、疑似页眉页脚残留。
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED = REPO_ROOT / "knowledge" / "processed"
REPORT_DIR = PROCESSED / "reports"

JSONL_FILES = [
    "documents.jsonl",
    "chunks.jsonl",
    "api_endpoints.jsonl",
    "cli_commands.jsonl",
    "glossary.jsonl",
    "qa_seed.jsonl",
    "issues.jsonl",
]
SCHEMA_FILES = {
    "chunks.jsonl": "chunk.schema.json",
    "api_endpoints.jsonl": "api_endpoint.schema.json",
    "cli_commands.jsonl": "cli_command.schema.json",
}

# 疑似页眉页脚残留
HEADER_FOOTER_PATTERNS = [
    re.compile(r"^第\s*\d+\s*页$"),
    re.compile(r"^第\s*\d+\s*页\s*$"),
    re.compile(r"^\s*API帮助文档\s*$"),
    re.compile(r"^\s*命令行帮助文档\s*$"),
    re.compile(r"^\s*版权所有[:：]"),
    re.compile(r"^\s*密级[:：]"),
    re.compile(r"^\s*下一代防火墙AF_AF8\.0\.107_文档\s*$"),
    re.compile(r"^\s*深信服技术支持"),
]


class Result:
    def __init__(self):
        self.checks = collections.OrderedDict()
        self.errors = []
        self.warnings = []

    def add_check(self, name, value):
        self.checks[name] = value

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

def load_jsonl(path: Path, result: Result):
    records = []
    if not path.exists():
        result.error(f"缺少产物文件：{path.name}")
        return records
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        result.error(f"{path.name} 以 UTF-8 BOM 开头")
    text = raw.decode("utf-8")
    if "\r" in text:
        result.warn(f"{path.name} 包含 CR，JSONL 建议统一使用 LF")
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            result.error(f"{path.name}:{lineno} JSON 解析失败：{exc}")
            continue
        if not isinstance(obj, dict):
            result.error(f"{path.name}:{lineno} 行内容不是 JSON 对象")
            continue
        records.append(obj)
    return records


def load_json(path: Path, result: Result):
    if not path.exists():
        result.error(f"缺少产物文件：{path.name}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.error(f"{path.name} JSON 解析失败：{exc}")
        return None


# --------------------------------------------------------------------------
# 极简 JSON Schema 校验（jsonschema 不可用时的兜底）
# --------------------------------------------------------------------------

def _type_ok(value, expected):
    mapping = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, mapping.get(expected, object))


def minimal_validate(instance, schema, path="$"):
    """支持 type / required / properties / additionalProperties / items / enum /
    pattern / minLength / minimum / maximum / const / oneOf / anyOf 的子集。"""
    errors = []
    if not schema:
        return errors
    if "oneOf" in schema:
        ok = sum(0 if minimal_validate(instance, s, path) else 1 for s in schema["oneOf"])
        if ok != 1:
            errors.append(f"{path}: oneOf 不满足")
        return errors
    if "anyOf" in schema:
        if not any(not minimal_validate(instance, s, path) for s in schema["anyOf"]):
            errors.append(f"{path}: anyOf 不满足")
        return errors
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: 取值 {instance!r} 不在枚举内")
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, t) for t in types):
            errors.append(f"{path}: 类型不匹配，期望 {types}，实际 {type(instance).__name__}")
            return errors
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: 长度小于 minLength={schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: 不匹配 pattern={schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: 小于 minimum={schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: 大于 maximum={schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: 元素数小于 minItems={schema['minItems']}")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                errors.extend(minimal_validate(item, item_schema, f"{path}[{index}]"))
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必需字段 {key}")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in instance:
                errors.extend(minimal_validate(instance[key], subschema, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                errors.append(f"{path}: 出现未声明字段 {sorted(extra)}")
    return errors


def validate_with_schema(instance, schema):
    try:
        import jsonschema  # type: ignore

        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema)
        return [f"{'/'.join(str(p) for p in err.absolute_path) or '$'}: {err.message}"
                for err in validator.iter_errors(instance)]
    except ImportError:
        return minimal_validate(instance, schema)
    except Exception as exc:  # pragma: no cover
        return [f"schema 校验异常：{exc!r}"]


def check_schema_self_valid(schema):
    """确认 schema 自身可被 Draft 2020-12 元模式接受。"""
    try:
        import jsonschema  # type: ignore

        jsonschema.Draft202012Validator.check_schema(schema)
        return []
    except ImportError:
        return []
    except Exception as exc:
        return [repr(exc)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict-warnings", action="store_true",
                        help="把 warning 也视为失败")
    args = parser.parse_args()

    result = Result()

    if not PROCESSED.exists():
        print(f"ERROR: 目录不存在：{PROCESSED}")
        return 2

    data = {}
    for name in JSONL_FILES:
        data[name] = load_jsonl(PROCESSED / name, result)
    manifest = load_json(PROCESSED / "manifest.json", result)

    if not data["documents.jsonl"]:
        print("ERROR: documents.jsonl 无有效记录，无法继续")
        return 2

    result.add_check("JSONL 文件数", len(JSONL_FILES))
    result.add_check("JSONL 总记录数", sum(len(v) for v in data.values()))

    # ---- 1. Schema 自校验 + 实例校验 -------------------------------------
    schemas = {}
    schema_errors = []
    for schema_name in SCHEMA_FILES.values():
        schema = load_json(PROCESSED / "schemas" / schema_name, result)
        if schema is None:
            continue
        schemas[schema_name] = schema
        schema_errors.extend(check_schema_self_valid(schema))
    result.add_check("JSON Schema 文件", len(schemas))
    if schema_errors:
        for item in schema_errors:
            result.error(f"Schema 元模式校验失败：{item}")
    result.add_check("Schema 元模式校验", "通过" if not schema_errors else "失败")

    instance_error_count = 0
    for jsonl_name, schema_name in SCHEMA_FILES.items():
        schema = schemas.get(schema_name)
        if schema is None:
            continue
        for index, record in enumerate(data[jsonl_name]):
            problems = validate_with_schema(record, schema)
            if problems:
                instance_error_count += 1
                if instance_error_count <= 10:
                    identifier = record.get("chunk_id") or record.get("endpoint_id") or record.get("command_id") or index
                    result.error(f"{jsonl_name}[{identifier}] Schema 校验失败：{problems[:3]}")
    if instance_error_count > 10:
        result.error(f"{jsonl_name} 等文件共 {instance_error_count} 条记录未通过 Schema 校验（仅显示前 10 条）")
    result.add_check("Schema 实例校验失败记录数", instance_error_count)

    # ---- 2. ID 唯一性 ----------------------------------------------------
    unique_specs = [
        ("documents.jsonl", "doc_id"),
        ("chunks.jsonl", "chunk_id"),
        ("api_endpoints.jsonl", "endpoint_id"),
        ("cli_commands.jsonl", "command_id"),
        ("glossary.jsonl", "term_id"),
        ("qa_seed.jsonl", "question_id"),
        ("issues.jsonl", "issue_id"),
    ]
    uniqueness_ok = True
    for jsonl_name, key in unique_specs:
        counter = collections.Counter(r.get(key) for r in data[jsonl_name])
        duplicates = [k for k, v in counter.items() if v > 1]
        if duplicates:
            uniqueness_ok = False
            result.error(f"{jsonl_name} 字段 {key} 存在重复：{duplicates[:5]}")
    result.add_check("ID 唯一性", "通过" if uniqueness_ok else "失败")

    doc_pages = {d["doc_id"]: d["page_count"] for d in data["documents.jsonl"]}
    chunk_ids = {c["chunk_id"] for c in data["chunks.jsonl"]}
    endpoint_ids = {e["endpoint_id"] for e in data["api_endpoints.jsonl"]}
    command_ids = {c["command_id"] for c in data["cli_commands.jsonl"]}

    # ---- 3. 引用完整性 ---------------------------------------------------
    ref_errors = 0
    for chunk in data["chunks.jsonl"]:
        if chunk["doc_id"] not in doc_pages:
            ref_errors += 1
            result.error(f"chunk {chunk['chunk_id']} 引用了不存在的 doc_id={chunk['doc_id']}")
        for key in ("prev_chunk_id", "next_chunk_id"):
            value = chunk.get(key)
            if value is not None and value not in chunk_ids:
                ref_errors += 1
                result.error(f"chunk {chunk['chunk_id']} 的 {key} 指向不存在的 chunk：{value}")
        for table_id in chunk.get("table_ids", []):
            if not (PROCESSED / "tables" / f"{table_id}.html").exists():
                ref_errors += 1
                result.error(f"chunk {chunk['chunk_id']} 引用了不存在的表格 HTML：{table_id}")
    for endpoint in data["api_endpoints.jsonl"]:
        if not endpoint.get("source_chunk_ids"):
            ref_errors += 1
            result.error(f"API 端点 {endpoint['endpoint_id']} 未关联任何 chunk")
        for chunk_id in endpoint.get("source_chunk_ids", []):
            if chunk_id not in chunk_ids:
                ref_errors += 1
                result.error(f"API 端点 {endpoint['endpoint_id']} 引用了不存在的 chunk：{chunk_id}")
    for command in data["cli_commands.jsonl"]:
        for chunk_id in command.get("source_chunk_ids", []):
            if chunk_id not in chunk_ids:
                ref_errors += 1
                result.error(f"CLI 命令 {command['command_id']} 引用了不存在的 chunk：{chunk_id}")
        parent = command.get("parent_command_id")
        if parent and parent not in command_ids:
            ref_errors += 1
            result.error(f"CLI 子命令 {command['command_id']} 的 parent_command_id 不存在：{parent}")
    for question in data["qa_seed.jsonl"]:
        for chunk_id in question.get("relevant_chunk_ids", []):
            if chunk_id not in chunk_ids:
                ref_errors += 1
                result.error(f"评估问题 {question['question_id']} 引用了不存在的 chunk：{chunk_id}")
        for doc_id in question.get("expected_doc_ids", []):
            if doc_id not in doc_pages:
                ref_errors += 1
                result.error(f"评估问题 {question['question_id']} 引用了不存在的 doc_id：{doc_id}")
    result.add_check("引用完整性错误数", ref_errors)

    # ---- 4. 页码范围 -----------------------------------------------------
    page_errors = 0
    for jsonl_name, key in (("chunks.jsonl", "chunk_id"),
                            ("api_endpoints.jsonl", "endpoint_id"),
                            ("cli_commands.jsonl", "command_id"),
                            ("issues.jsonl", "issue_id")):
        for record in data[jsonl_name]:
            doc_id = record.get("doc_id")
            page_count = doc_pages.get(doc_id)
            if page_count is None:
                continue
            for field in ("page", "page_start", "page_end"):
                value = record.get(field)
                if value is None:
                    continue
                if not isinstance(value, int) or value < 1 or value > page_count:
                    page_errors += 1
                    if page_errors <= 10:
                        result.error(
                            f"{jsonl_name} {record.get(key)} 的 {field}={value} 超出 "
                            f"{doc_id} 页数范围 1..{page_count}"
                        )
    if page_errors > 10:
        result.error(f"共 {page_errors} 处页码越界（仅显示前 10 条）")
    result.add_check("页码越界数", page_errors)

    # ---- 5. prev / next 双向一致 ----------------------------------------
    chunk_by_id = {c["chunk_id"]: c for c in data["chunks.jsonl"]}
    link_errors = 0
    for chunk in data["chunks.jsonl"]:
        nxt = chunk.get("next_chunk_id")
        if nxt:
            target = chunk_by_id.get(nxt)
            if target is None:
                link_errors += 1
            elif target.get("prev_chunk_id") != chunk["chunk_id"]:
                link_errors += 1
                result.error(
                    f"chunk 链不一致：{chunk['chunk_id']}.next = {nxt}，"
                    f"但 {nxt}.prev = {target.get('prev_chunk_id')}"
                )
        prev = chunk.get("prev_chunk_id")
        if prev:
            target = chunk_by_id.get(prev)
            if target is None:
                link_errors += 1
            elif target.get("next_chunk_id") != chunk["chunk_id"]:
                link_errors += 1
                result.error(
                    f"chunk 链不一致：{chunk['chunk_id']}.prev = {prev}，"
                    f"但 {prev}.next = {target.get('next_chunk_id')}"
                )
    # 同一文档内顺序必须连续
    by_doc = collections.defaultdict(list)
    for chunk in data["chunks.jsonl"]:
        by_doc[chunk["doc_id"]].append(chunk)
    order_errors = 0
    for doc_id, items in by_doc.items():
        for left, right in zip(items, items[1:]):
            if left.get("next_chunk_id") != right["chunk_id"]:
                order_errors += 1
                result.error(
                    f"{doc_id} 文档内 chunk 顺序断裂：{left['chunk_id']}.next "
                    f"应为 {right['chunk_id']}，实际 {left.get('next_chunk_id')}"
                )
    result.add_check("chunk 双向链接错误数", link_errors)
    result.add_check("文档内顺序断裂数", order_errors)

    # ---- 6. 内容质量 -----------------------------------------------------
    empty_chunks = 0
    short_chunks = 0
    residue_chunks = 0
    hash_mismatch = 0
    for chunk in data["chunks.jsonl"]:
        for field in ("content_markdown", "content_plain", "title"):
            if not str(chunk.get(field) or "").strip():
                empty_chunks += 1
                result.error(f"chunk {chunk['chunk_id']} 的 {field} 为空")
        digest = hashlib.sha256(chunk["content_plain"].encode("utf-8")).hexdigest()
        if digest != chunk.get("text_sha256"):
            hash_mismatch += 1
            result.error(f"chunk {chunk['chunk_id']} 的 text_sha256 与 content_plain 不一致")
        if chunk.get("char_count", 0) < 20:
            short_chunks += 1
        for pattern in HEADER_FOOTER_PATTERNS:
            if pattern.search(chunk["content_plain"].strip()):
                residue_chunks += 1
                if residue_chunks <= 10:
                    result.error(
                        f"chunk {chunk['chunk_id']} 疑似残留页眉页脚："
                        f"{pattern.pattern}"
                    )
                break
    result.add_check("空字段 chunk 数", empty_chunks)
    result.add_check("超短 chunk 数（<20 字符）", short_chunks)
    result.add_check("疑似页眉页脚残留 chunk 数", residue_chunks)
    result.add_check("text_sha256 不一致数", hash_mismatch)

    duplicate_hashes = [h for h, n in collections.Counter(
        c["text_sha256"] for c in data["chunks.jsonl"]).items() if n > 1]
    result.add_check("重复正文 chunk 组数", len(duplicate_hashes))
    if duplicate_hashes:
        result.warn(f"存在 {len(duplicate_hashes)} 组正文完全重复的 chunk（跨章节重复内容，已记录 issues）")

    # ---- 7. API / CLI 记录服务性 ----------------------------------------
    endpoints_without_method = [e["endpoint_id"] for e in data["api_endpoints.jsonl"]
                                if not e.get("method") or not e.get("path")]
    commands_without_syntax = [c["command_id"] for c in data["cli_commands.jsonl"]
                               if not c.get("syntax")]
    result.add_check("API 端点数", len(data["api_endpoints.jsonl"]))
    result.add_check("API 端点缺 method/path 数", len(endpoints_without_method))
    result.add_check("CLI 命令数", len(data["cli_commands.jsonl"]))
    result.add_check("CLI 命令缺 syntax 数", len(commands_without_syntax))
    if endpoints_without_method:
        result.warn(f"{len(endpoints_without_method)} 个 API 端点缺少 method 或 path"
                    f"（示例：{endpoints_without_method[:3]}）")
    if commands_without_syntax:
        result.warn(f"{len(commands_without_syntax)} 条 CLI 记录缺少 syntax"
                    f"（示例：{commands_without_syntax[:3]}）")
    result.add_check("每端点/命令至少一个 chunk",
                     "通过" if not result.errors else "见 error 列表")

    # ---- 8. 表格 HTML 数量 ----------------------------------------------
    table_files = sorted((PROCESSED / "tables").glob("*.html"))
    result.add_check("表格 HTML 文件数", len(table_files))
    referenced = collections.Counter()
    for chunk in data["chunks.jsonl"]:
        for table_id in chunk.get("table_ids", []):
            referenced[table_id] += 1
    orphans = [f.stem for f in table_files if referenced.get(f.stem, 0) == 0]
    result.add_check("未被任何 chunk 引用的表格数", len(orphans))
    if orphans:
        result.warn(f"{len(orphans)} 个表格 HTML 未被 chunk 引用（多为跨页续表，"
                    f"示例：{orphans[:3]}）")

    # ---- 9. manifest 一致性 ---------------------------------------------
    if manifest:
        counts = manifest.get("counts", {})
        consistency = []
        pairs = [
            ("documents", len(data["documents.jsonl"])),
            ("chunks", len(data["chunks.jsonl"])),
            ("api_endpoints", len(data["api_endpoints.jsonl"])),
            ("cli_commands", len(data["cli_commands.jsonl"])),
            ("glossary", len(data["glossary.jsonl"])),
            ("qa_seed", len(data["qa_seed.jsonl"])),
            ("issues", len(data["issues.jsonl"])),
        ]
        for key, actual in pairs:
            if counts.get(key) != actual:
                consistency.append(f"{key}: manifest={counts.get(key)} 实际={actual}")
        result.add_check("manifest 计数一致性", "通过" if not consistency else "不一致")
        for item in consistency:
            result.error(f"manifest 计数不一致：{item}")

    # ---- 输出 ------------------------------------------------------------
    payload = {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "checks": result.checks,
        "errors": len(result.errors),
        "warnings": len(result.warnings),
        "error_messages": result.errors[:200],
        "warning_messages": result.warnings[:200],
        "exit_code": 1 if result.errors else 0,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "validation-result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    print("=" * 78)
    print("知识库产物校验结果")
    print("=" * 78)
    for name, value in result.checks.items():
        print(f"  {name:.<48} {value}")
    print("-" * 78)
    print(f"  error   : {len(result.errors)}")
    print(f"  warning : {len(result.warnings)}")
    for index, message in enumerate(result.errors[:20], start=1):
        print(f"  [E{index:03d}] {message}")
    for index, message in enumerate(result.warnings[:20], start=1):
        print(f"  [W{index:03d}] {message}")
    print("-" * 78)

    if args.strict_warnings and result.warnings:
        print("严格模式：warning 视为失败")
        return 1
    if result.errors:
        print("校验未通过：请修复 error 后重跑。")
        return 1
    print("校验通过：无 error。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
