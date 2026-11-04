from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import jieba

jieba.setLogLevel(20)

_TECHNICAL = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{1,}|\d{2,}(?:\.\d+)*")
_CJK = re.compile(r"[\u3400-\u9fff]")
_STOPWORDS = {
    "如何", "怎么", "什么", "是否", "可以", "一个", "这个", "那个", "哪些", "进行",
    "使用", "需要", "以及", "相关", "通过", "查询", "获取", "配置", "设置",
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name}:{line_number} 不是合法 JSON") from exc


def tokenize(text: str) -> list[str]:
    """Chinese search tokens plus exact technical identifiers."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    terms: list[str] = []
    seen: set[str] = set()
    for term in [*_TECHNICAL.findall(normalized), *jieba.cut_for_search(normalized)]:
        term = term.strip().lower()
        if not term or term in _STOPWORDS:
            continue
        if _CJK.search(term) and len(term) < 2:
            continue
        if not _CJK.search(term) and len(term) < 2:
            continue
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _fts_query(query: str) -> str:
    terms = tokenize(query)[:32]
    if not terms:
        return ""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _schema_type(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return value["type"]
    return None


def _flatten_schema_fields(schema: dict | None) -> list[dict]:
    fields: list[dict] = []

    def walk(node, prefix=""):
        if not isinstance(node, dict):
            return
        required = node.get("required") if isinstance(node.get("required"), list) else None
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        for name, child in properties.items():
            if not isinstance(child, dict):
                continue
            path = f"{prefix}.{name}" if prefix else name
            marker = child.get("description")
            item = {
                "name": path,
                "type": _schema_type(child.get("type")),
                "required_by_schema": (name in required) if required is not None else None,
            }
            if marker not in (None, "", "--"):
                item["source_description"] = str(marker)[:240]
            for source, target in (("enum", "enum"), ("minimum", "minimum"), ("maximum", "maximum"),
                                   ("minLength", "min_length"), ("maxLength", "max_length")):
                if source in child:
                    item[target] = child[source]
            fields.append(item)
            walk(child, path)

    walk(schema or {})
    return fields


def _compact_endpoint(raw: dict) -> dict:
    request_fields = _flatten_schema_fields(raw.get("request_body_schema"))
    response_fields = _flatten_schema_fields(raw.get("response_schema"))
    source_chunks = raw.get("source_chunk_ids") or []
    return {
        "endpoint_id": raw["endpoint_id"], "title": raw.get("title"),
        "method": raw.get("method"), "path": raw.get("path"), "summary": raw.get("summary"),
        "page_start": raw.get("page_start"), "page_end": raw.get("page_end"),
        "query_parameters": raw.get("query_parameters") or [],
        "path_parameters": raw.get("path_parameters") or [],
        "request_field_count": len(request_fields),
        "request_top_level_fields": [f["name"] for f in request_fields if "." not in f["name"]][:30],
        "response_field_count": len(response_fields),
        "source_chunk_ids": source_chunks[:3], "source_chunk_count": len(source_chunks),
        "evidence": {
            "auth": raw.get("auth"), "headers": raw.get("headers") or [],
            "success_codes": raw.get("success_codes"), "error_codes": raw.get("error_codes"),
        },
        "caveats": [
            "auth、headers 或返回码为 null/空数组时，表示该端点小节未提供，不能自行补全。",
            "required_by_schema 为 null 时，表示结构化数据未确认必填性，不能推断为必填或选填。",
            "使用 get_api_endpoint(endpoint_id, section, offset, limit) 分页读取字段或样例。",
        ],
    }


class KnowledgeRepository:
    def __init__(self, database):
        self.database = database

    def _connect(self):
        connection = sqlite3.connect(self.database.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def import_processed(self, processed_dir: Path) -> dict:
        processed_dir = processed_dir.resolve()
        required = [
            "manifest.json", "documents.jsonl", "chunks.jsonl",
            "api_endpoints.jsonl", "cli_commands.jsonl",
        ]
        missing = [name for name in required if not (processed_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(f"知识库产物缺失：{', '.join(missing)}")

        manifest_path = processed_dir / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        documents = list(_read_jsonl(processed_dir / "documents.jsonl"))
        chunks = list(_read_jsonl(processed_dir / "chunks.jsonl"))
        endpoints = list(_read_jsonl(processed_dir / "api_endpoints.jsonl"))
        commands = list(_read_jsonl(processed_dir / "cli_commands.jsonl"))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM knowledge_fts")
            connection.execute("DELETE FROM api_endpoints")
            connection.execute("DELETE FROM cli_commands")
            connection.execute("DELETE FROM chunks")
            connection.execute("DELETE FROM documents")

            connection.executemany(
                """INSERT INTO documents
                   (doc_id,title,doc_type,vendor,product,version,source_path,sha256,page_count,processed_at,raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(d["doc_id"], d["title"], d["doc_type"], d.get("vendor"), d.get("product"),
                  d.get("version"), d.get("source_path"), d["sha256"], d["page_count"],
                  d.get("processed_at"), _json(d)) for d in documents],
            )

            for offset in range(0, len(chunks), 250):
                batch = chunks[offset:offset + 250]
                connection.executemany(
                    """INSERT INTO chunks
                       (chunk_id,doc_id,doc_type,product,version,title,title_path_json,page_start,page_end,
                        content_markdown,content_plain,keywords_json,entities_json,char_count,table_ids_json,
                        has_code,prev_chunk_id,next_chunk_id,text_sha256)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(c["chunk_id"], c["doc_id"], c["doc_type"], c.get("product"), c.get("version"),
                      c.get("title"), _json(c.get("title_path", [])), c["page_start"], c["page_end"],
                      c["content_markdown"], c["content_plain"], _json(c.get("keywords", [])),
                      _json(c.get("entities", [])), c.get("char_count", len(c["content_plain"])),
                      _json(c.get("table_ids", [])), int(bool(c.get("has_code"))), c.get("prev_chunk_id"),
                      c.get("next_chunk_id"), c["text_sha256"]) for c in batch],
                )
                connection.executemany(
                    "INSERT INTO knowledge_fts(chunk_id,title_tokens,content_tokens,keyword_tokens) VALUES (?,?,?,?)",
                    [(c["chunk_id"], " ".join(tokenize(" ".join(c.get("title_path", [])))),
                      " ".join(tokenize(c["content_plain"])),
                      " ".join(tokenize(" ".join(c.get("keywords", []))))) for c in batch],
                )

            connection.executemany(
                """INSERT INTO api_endpoints
                   (endpoint_id,doc_id,title,method,path,summary,page_start,page_end,source_chunk_ids_json,raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(e["endpoint_id"], e["doc_id"], e.get("title"), e.get("method"), e.get("path"),
                  e.get("summary"), e.get("page_start"), e.get("page_end"),
                  _json(e.get("source_chunk_ids", [])), _json(e)) for e in endpoints],
            )
            connection.executemany(
                """INSERT INTO cli_commands
                   (command_id,doc_id,command,syntax,mode,summary,page_start,page_end,source_chunk_ids_json,raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(c["command_id"], c["doc_id"], c["command"], c.get("syntax"), c.get("mode"),
                  c.get("summary"), c.get("page_start"), c.get("page_end"),
                  _json(c.get("source_chunk_ids", [])), _json(c)) for c in commands],
            )
            connection.execute(
                """INSERT INTO knowledge_imports
                   (source_manifest_sha256,imported_at,documents,chunks,api_endpoints,cli_commands)
                   VALUES (?,?,?,?,?,?)""",
                (manifest_sha, datetime.now(timezone.utc).isoformat(), len(documents), len(chunks),
                 len(endpoints), len(commands)),
            )
            connection.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('optimize')")

        return {
            "manifest_sha256": manifest_sha,
            "documents": len(documents),
            "chunks": len(chunks),
            "api_endpoints": len(endpoints),
            "cli_commands": len(commands),
            "source_counts": manifest.get("counts", {}),
        }

    def stats(self) -> dict:
        with self._connect() as connection:
            counts = {}
            for table in ("documents", "chunks", "api_endpoints", "cli_commands"):
                counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            last = connection.execute(
                "SELECT imported_at, source_manifest_sha256 FROM knowledge_imports ORDER BY import_id DESC LIMIT 1"
            ).fetchone()
        return {**counts, "indexed": counts["chunks"] > 0,
                "last_imported_at": last["imported_at"] if last else None,
                "manifest_sha256": last["source_manifest_sha256"] if last else None,
                "retrieval": "SQLite FTS5 + jieba BM25"}

    def overview(self) -> dict:
        """Return bounded, operational metadata for the knowledge management UI."""
        with self._connect() as connection:
            documents = connection.execute("""
                SELECT doc_id,title,doc_type,vendor,product,version,source_path,
                       sha256,page_count,processed_at
                FROM documents ORDER BY doc_type,title
            """).fetchall()
            items = []
            for row in documents:
                item = dict(row)
                item["chunks"] = connection.execute(
                    "SELECT count(*) FROM chunks WHERE doc_id=?", (row["doc_id"],)
                ).fetchone()[0]
                item["api_endpoints"] = connection.execute(
                    "SELECT count(*) FROM api_endpoints WHERE doc_id=?", (row["doc_id"],)
                ).fetchone()[0]
                item["cli_commands"] = connection.execute(
                    "SELECT count(*) FROM cli_commands WHERE doc_id=?", (row["doc_id"],)
                ).fetchone()[0]
                items.append(item)
            imports = [dict(row) for row in connection.execute(
                """SELECT import_id,source_manifest_sha256,imported_at,documents,chunks,
                          api_endpoints,cli_commands
                   FROM knowledge_imports ORDER BY import_id DESC LIMIT 10"""
            ).fetchall()]
            fts_rows = connection.execute("SELECT count(*) FROM knowledge_fts").fetchone()[0]
            chunk_rows = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        stats = self.stats()
        return {
            "stats": stats,
            "documents": items,
            "imports": imports,
            "index_health": {
                "engine": "SQLite FTS5",
                "tokenizer": "jieba + unicode61",
                "chunk_rows": chunk_rows,
                "fts_rows": fts_rows,
                "healthy": chunk_rows > 0 and chunk_rows == fts_rows,
                "coverage_percent": round(fts_rows / chunk_rows * 100, 2) if chunk_rows else 0,
            },
        }

    def search(self, query: str, top_k: int = 5, doc_type: str | None = None,
               doc_id: str | None = None, version: str | None = None) -> list[dict]:
        match = _fts_query(query)
        if not match:
            return []
        where = ["knowledge_fts MATCH ?"]
        params: list = [match]
        if doc_type:
            where.append("c.doc_type = ?")
            params.append(doc_type)
        if doc_id:
            where.append("c.doc_id = ?")
            params.append(doc_id)
        if version:
            where.append("c.version = ?")
            params.append(version)
        candidate_limit = min(max(top_k * 12, 40), 240)
        sql = f"""
            SELECT c.*, bm25(knowledge_fts, 0.0, 8.0, 1.0, 5.0) AS bm25_rank
            FROM knowledge_fts
            JOIN chunks c ON c.chunk_id = knowledge_fts.chunk_id
            WHERE {' AND '.join(where)}
            ORDER BY bm25_rank ASC
            LIMIT ?
        """
        params.append(candidate_limit)
        query_lower = query.lower().strip()
        query_terms = tokenize(query)
        with self._connect() as connection:
            candidates = connection.execute(sql, params).fetchall()

        ranked = []
        seen_hashes = set()
        for row in candidates:
            if row["text_sha256"] in seen_hashes:
                continue
            seen_hashes.add(row["text_sha256"])
            content_lower = row["content_plain"].lower()
            title_lower = (row["title"] or "").lower()
            coverage = sum(term in content_lower or term in title_lower for term in query_terms) / max(1, len(query_terms))
            exact_bonus = 2.0 if query_lower and query_lower in content_lower else 0.0
            title_bonus = 1.0 if query_lower and query_lower in title_lower else 0.0
            score = exact_bonus + title_bonus + coverage + min(1.0, abs(float(row["bm25_rank"])))
            position = min((content_lower.find(term) for term in query_terms if term in content_lower), default=0)
            start = max(0, position - 100)
            end = min(len(row["content_plain"]), start + 700)
            snippet = row["content_plain"][start:end].strip()
            ranked.append({
                "chunk_id": row["chunk_id"], "doc_id": row["doc_id"], "doc_type": row["doc_type"],
                "version": row["version"], "title": row["title"],
                "title_path": json.loads(row["title_path_json"]), "page_start": row["page_start"],
                "page_end": row["page_end"], "score": round(score, 6), "snippet": snippet,
                "keywords": json.loads(row["keywords_json"]), "prev_chunk_id": row["prev_chunk_id"],
                "next_chunk_id": row["next_chunk_id"], "match_type": "fts5_bm25",
            })
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[:top_k]

    def get_chunk(self, chunk_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for field in ("title_path_json", "keywords_json", "entities_json", "table_ids_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        result["has_code"] = bool(result["has_code"])
        return result

    def lookup_api(self, query: str, method: str | None = None, limit: int = 10) -> list[dict]:
        """Find endpoint candidates by URL, title, or summary."""
        query = query.strip()
        normalized = query
        for source, target in {"编辑": "修改", "查看": "查询", "新建": "创建"}.items():
            normalized = normalized.replace(source, target)
        terms = list(dict.fromkeys([query.lower(), normalized.lower(), *tokenize(normalized)]))
        terms = [term for term in terms if term]
        where: list[str] = []
        params: list = []
        if method:
            where.append("method = ?")
            params.append(method.upper())
        with self._connect() as connection:
            candidates = connection.execute(
                f"SELECT endpoint_id, title, summary, path FROM api_endpoints"
                + (f" WHERE {' AND '.join(where)}" if where else ""),
                params,
            ).fetchall()

            ranked: list[tuple[float, str]] = []
            for row in candidates:
                path_text = (row["path"] or "").lower()
                title_text = (row["title"] or "").lower()
                summary_text = (row["summary"] or "").lower()
                haystack = f"{title_text}\n{summary_text}\n{path_text}"
                matched = [term for term in terms if term in haystack]
                if not matched:
                    continue
                score = sum(2.0 for term in terms if term in title_text)
                score += sum(1.0 for term in terms if term in summary_text)
                score += sum(0.5 for term in terms if term in path_text)
                score += max(0, 10 - path_text.count("/")) * 0.1
                if query.lower() == path_text:
                    score += 20
                elif query.lower() in path_text:
                    score += 8
                if query.lower() in title_text or normalized.lower() in title_text:
                    score += 6
                if query.lower() in summary_text or normalized.lower() in summary_text:
                    score += 4
                if "接口" in normalized and re.search(
                    r"/namespaces/@namespace/interfaces(?:/|$)", path_text
                ):
                    score += 20
                ranked.append((score, row["endpoint_id"]))

            ranked.sort(key=lambda item: (-item[0], item[1]))
            endpoint_ids = [endpoint_id for _, endpoint_id in ranked[:limit]]
            rows = [connection.execute(
                "SELECT raw_json FROM api_endpoints WHERE endpoint_id=?", (endpoint_id,)
            ).fetchone() for endpoint_id in endpoint_ids]
        return [_compact_endpoint(json.loads(row[0])) for row in rows if row]

    def get_api_endpoint(self, endpoint_id: str, section: str = "overview",
                         offset: int = 0, limit: int = 40) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT raw_json FROM api_endpoints WHERE endpoint_id=?", (endpoint_id,)
            ).fetchone()
        if not row:
            return None
        raw = json.loads(row[0])
        base = _compact_endpoint(raw)
        base["section"] = section
        if section == "overview":
            return base
        if section in ("request", "response"):
            schema_key = "request_body_schema" if section == "request" else "response_schema"
            fields = _flatten_schema_fields(raw.get(schema_key))
            base.update({"offset": offset, "limit": limit, "total_fields": len(fields),
                         "fields": fields[offset:offset + limit], "has_more": offset + limit < len(fields)})
            return base
        if section == "examples":
            def trim(values):
                result = []
                for value in values or []:
                    example = str(value)
                    result.append({"text": example[:4000], "truncated": len(example) > 4000})
                return result[:3]
            base.update({"request_examples": trim(raw.get("request_examples")),
                         "response_examples": trim(raw.get("response_examples")),
                         "error_examples": trim(raw.get("error_examples"))})
            return base
        raise ValueError("section must be overview, request, response, or examples")

    def lookup_cli(self, command: str, limit: int = 10) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT raw_json FROM cli_commands
                   WHERE command LIKE ? OR syntax LIKE ?
                   ORDER BY CASE WHEN command = ? THEN 0 ELSE 1 END, command LIMIT ?""",
                (f"%{command}%", f"%{command}%", command, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
