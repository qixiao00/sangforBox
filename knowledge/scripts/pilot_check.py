"""开发期小样验证工具（非产物文件）。

在耗时较长的全量构建之前，用少量代表性页面跑一遍完整解析链路，
用于快速定位标题匹配、表格提取、代码块识别与命令/端点抽取的问题。

用法：
    python knowledge/scripts/pilot_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_knowledge as bk  # noqa: E402

# 每份文档选取有代表性的页面：文档说明、端点样例、附录、子命令、缩略语等
PILOT = {
    "af-8.0.107-api": [42, 43, 48, 51, 52, 55, 56, 57, 58, 3294],
    "af-8.0.107-cli": [18, 19, 21, 22, 26, 28, 811],
    "af-8.0.107-product": [3, 9, 17, 22, 686, 721, 722],
}


def main() -> int:
    bk.ISSUES.clear()
    for spec in bk.DOC_SPECS:
        pages = PILOT[spec["doc_id"]]
        page_filter = set(pages)
        print("=" * 70)
        print(f"PILOT {spec['doc_id']}  pages={sorted(pages)}")
        parsed = bk.parse_document(spec, None, page_filter=page_filter)
        parsed["profile"] = spec["profile"]
        parsed["vocabulary"] = bk.collect_vocabulary([parsed])
        parsed["reserved"] = {
            s["title"]: True for s in bk.iter_sections(parsed["root"]) if s["title"]
        }
        chunks, title_index = bk.build_chunks(parsed, spec)
        print(f"  elements={len(parsed['elements'])} chunks={len(chunks)}")
        print("  --- first 40 elements ---")
        for element in parsed["elements"][:40]:
            detail = element.get("text", "")[:60].replace("\n", " / ")
            if element["kind"] == "table":
                detail = "header=" + " | ".join(element["header"])
            if element["kind"] == "heading":
                print(f"  [h{element['level']}] p{element['page']} {detail}")
            else:
                print(f"  ({element['kind']}) p{element['page']} {detail}")
        print("  --- chunks ---")
        for chunk in chunks[:8]:
            print(
                f"  {chunk['chunk_id']} p{chunk['page_start']}-{chunk['page_end']} "
                f"len={chunk['char_count']} :: {' / '.join(chunk['title_path'])}"
            )
        if spec["doc_type"] == "api_manual":
            endpoints = bk.extract_api_endpoints(parsed, spec, title_index)
            print(f"  --- endpoints={len(endpoints)} ---")
            for endpoint in endpoints[:5]:
                print(json.dumps(
                    {
                        "id": endpoint["endpoint_id"],
                        "title": endpoint["title"],
                        "method": endpoint["method"],
                        "path": endpoint["path"],
                        "summary": (endpoint["summary"] or "")[:50],
                        "query": [p["name"] for p in endpoint["query_parameters"]],
                        "body": len((endpoint["request_body_schema"] or {}).get("x-flat-parameters", [])),
                        "resp": len((endpoint["response_schema"] or {}).get("x-flat-parameters", [])),
                        "req_ex": len(endpoint["request_examples"]),
                        "succ_ex": len(endpoint["response_examples"]),
                        "fail_ex": len(endpoint["error_examples"]),
                        "constraints": endpoint["constraints"],
                        "chunks": len(endpoint["source_chunk_ids"]),
                    },
                    ensure_ascii=False,
                ))
        if spec["doc_type"] == "cli_manual":
            commands, sub_total = bk.extract_cli_commands(parsed, spec, title_index)
            print(f"  --- cli records={len(commands)} sub={sub_total} ---")
            for command in commands[:8]:
                print(json.dumps(
                    {
                        "id": command["command_id"],
                        "command": command["command"],
                        "syntax": command["syntax"],
                        "summary": (command["summary"] or "")[:40],
                        "mode": command["mode"],
                        "args": len(command["arguments"] or []),
                        "examples": (command["examples"] or [])[:2],
                        "related": command["related_commands"],
                    },
                    ensure_ascii=False,
                ))
    print("=" * 70)
    print(f"issues={len(bk.ISSUES)}")
    for issue in bk.ISSUES[:25]:
        print(" ", issue["severity"], issue["category"], issue["page"], issue["message"],
              "|", (issue.get("detail") or "")[:70])
    return 0


if __name__ == "__main__":
    sys.exit(main())
