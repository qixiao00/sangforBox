import asyncio
import json
import sqlite3
import re
import httpx2
from mcp.client.streamable_http import streamable_http_client
from pathlib import Path
from fastapi.testclient import TestClient
from mcp import Client
from af_app.app import create_app
from af_app.config import Settings
from af_app.database import Database
from af_app.knowledge import KnowledgeRepository

def test_framework(tmp_path):
    path = tmp_path / 'test.db'
    app = create_app(Settings(path))
    with TestClient(app) as web:
        home = web.get('/')
        assert home.status_code == 200
        assert 'SangforBox' in home.text
        asset = re.search(r'src="(/static/_next/static/[^"]+\.js)"', home.text)
        assert asset and web.get(asset.group(1)).status_code == 200
        assert web.post('/api/test',json={'message':''}).status_code == 422
        response = web.post('/api/test',json={'message':'中文测试'})
        assert response.status_code == 200, response.text
        assert response.json()['message'] == '中文测试'
        assert response.json()['schema_version'] == 5
        response = web.post('/api/test?stream=true',json={'message':'stream'})
        events = [json.loads(line) for line in response.text.splitlines()]
        assert [(e['actor'],e['status']) for e in events] == [('Web','running'),('MCP','running'),('SQLite','running'),('SQLite','completed'),('MCP','completed'),('Web','completed')]
        assert len({e['request_id'] for e in events}) == 1
        assert events[-1]['result']['message'] == 'stream'
        async def direct():
            async with Client(app.state.mcp_server) as client:
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == {
                    'framework_probe', 'search_knowledge', 'get_source_chunk',
                    'lookup_api_endpoint', 'get_api_endpoint', 'lookup_cli_command',
                    'query_device', 'edit_device', 'initialize_device',
                    'analyze_app_control', 'get_app_control_analysis', 'review_app_control', 'verify_app_control_review',
                    'preflight_app_control_review', 'record_app_control_feedback',
                    'get_app_control_feedback', 'list_app_control_feedback'
                }
                result = await client.call_tool('framework_probe', {'message':'direct'})
                assert result.structured_content['message'] == 'direct'
        asyncio.run(direct())
        async def remote():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                async with Client(streamable_http_client('http://127.0.0.1/mcp/',http_client=http)) as client:
                    assert {t.name for t in (await client.list_tools()).tools} == {
                        'framework_probe', 'search_knowledge', 'get_source_chunk',
                        'lookup_api_endpoint', 'get_api_endpoint', 'lookup_cli_command',
                        'query_device', 'edit_device', 'initialize_device',
                        'analyze_app_control', 'get_app_control_analysis', 'review_app_control', 'verify_app_control_review',
                        'preflight_app_control_review', 'record_app_control_feedback',
                        'get_app_control_feedback', 'list_app_control_feedback'
                    }
                    result = await client.call_tool('framework_probe', {'message':'http-transport'})
                    assert result.structured_content['message']=='http-transport'
        asyncio.run(remote())
    with sqlite3.connect(path) as db:
        assert db.execute('select version from schema_versions').fetchall() == [(1,), (2,), (3,), (4,), (5,)]
    with TestClient(create_app(Settings(path))) as web:
        assert web.post('/api/test',json={}).json()['schema_version']==5


def test_workbuddy_distribution_has_one_connector_and_three_scenario_skills():
    root = Path('connector/workbuddy')
    assert not (root/'connector-meta.json').exists()
    assert not (root/'mcp.json').exists()
    assert not (root/'optional-skills').exists()
    skills = {path.parent.name: path for path in (root/'skills').glob('*/SKILL.md')}
    assert set(skills) == {
        'af-device-initialization', 'af-app-control-optimization', 'af-security-operations'
    }
    for name, path in skills.items():
        content = path.read_text(encoding='utf-8-sig')
        assert f'name: {name}' in content
        assert 'layer: scenario' in content
        assert 'af-workbench' in content
    for markdown in root.rglob('*.md'):
        for target in re.findall(r'\]\(([^)#]+)(?:#[^)]+)?\)', markdown.read_text(encoding='utf-8-sig')):
            if '://' not in target:
                assert (markdown.parent / target).exists(), f"Broken link in {markdown}: {target}"


def test_download_is_also_persisted_to_output_directory(tmp_path, monkeypatch):
    output = tmp_path / "separate-results"
    app = create_app(Settings(tmp_path / "export.db", output_directory=output))
    monkeypatch.setattr(app.state.policy_optimizer.store, "export_csv", lambda _: "\ufeffname\r\nrule-1\r\n")
    with TestClient(app) as web:
        response = web.get("/api/app-control/analyses/analysis-1/review.csv")
    assert response.status_code == 200
    assert (output / "app-control-review-analysis-1.csv").read_text(encoding="utf-8") == "\ufeffname\nrule-1\n"


def test_failure_and_isolation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    app = create_app(Settings(tmp_path/'trace.db'))
    with TestClient(app) as web:
        def call(message):
            r = web.post('/api/test?stream=true', json={'message':message})
            return [json.loads(line) for line in r.text.splitlines()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first,second = list(pool.map(call,['first','second']))
        assert first[0]['request_id'] != second[0]['request_id']
        assert first[-1]['result']['message']=='first'
        assert second[-1]['result']['message']=='second'
        def broken():
            raise RuntimeError('private-internal-detail')
        monkeypatch.setattr(app.state.database,'probe',broken)
        events = call('failure')
        assert events[-1]['actor']=='Web' and events[-1]['status']=='failed'
        assert any(e['actor']=='SQLite' and e['status']=='failed' for e in events)
        assert 'private-internal-detail' not in json.dumps(events)
        assert web.post('/api/test',json={}).status_code==502


def test_knowledge_import_and_search(tmp_path):
    processed = tmp_path/'processed'
    processed.mkdir()
    document = {
        'doc_id':'af-test', 'title':'测试手册', 'doc_type':'product_manual', 'vendor':'深信服',
        'product':'AF', 'version':'8.0.107', 'source_path':'test.pdf', 'sha256':'a'*64,
        'page_count':10, 'processed_at':'2026-09-16T00:00:00Z'
    }
    chunk = {
        'chunk_id':'af-test::p3::t000001::n0001::0000000001', 'doc_id':'af-test',
        'doc_type':'product_manual', 'product':'AF', 'version':'8.0.107', 'title':'应用控制策略',
        'title_path':['策略','应用控制策略'], 'page_start':3, 'page_end':3,
        'content_markdown':'应用控制策略用于限制游戏网站访问。参数 `policy_id`。',
        'content_plain':'应用控制策略用于限制游戏网站访问。参数 policy_id。',
        'keywords':['应用控制','策略','policy_id'], 'entities':[], 'char_count':31,
        'table_ids':[], 'has_code':False, 'prev_chunk_id':None, 'next_chunk_id':None,
        'text_sha256':'b'*64
    }
    endpoint = {
        'endpoint_id':'ep-1', 'doc_id':'af-test', 'title':'查询策略', 'method':'GET',
        'path':'/api/policy', 'summary':'查询策略', 'page_start':3, 'page_end':3,
        'source_chunk_ids':[chunk['chunk_id']]
    }
    command = {
        'command_id':'cli-1', 'doc_id':'af-test', 'command':'show policy',
        'syntax':'show policy <id>', 'mode':'执行模式', 'summary':'查看策略',
        'page_start':4, 'page_end':4, 'source_chunk_ids':[chunk['chunk_id']]
    }
    def write(name, rows):
        (processed/name).write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')
    (processed/'manifest.json').write_text(json.dumps({'counts':{'chunks':1}}), encoding='utf-8')
    write('documents.jsonl',[document])
    write('chunks.jsonl',[chunk])
    write('api_endpoints.jsonl',[endpoint])
    write('cli_commands.jsonl',[command])
    database = Database(tmp_path/'knowledge.db')
    database.initialize()
    repository = KnowledgeRepository(database)
    imported = repository.import_processed(processed)
    assert imported['chunks']==1
    assert repository.search('如何限制游戏网站', 5)[0]['chunk_id']==chunk['chunk_id']
    compact = repository.lookup_api('/api/policy','GET')[0]
    assert compact['endpoint_id']=='ep-1' and 'request_body_schema' not in compact
    assert repository.lookup_api('查询策略')[0]['endpoint_id']=='ep-1'
    detail = repository.get_api_endpoint('ep-1','examples')
    assert detail['section']=='examples' and detail['page_start']==3
    assert repository.lookup_cli('show policy')[0]['command_id']=='cli-1'
    assert repository.stats()['indexed'] is True
    overview = repository.overview()
    assert overview['documents'][0]['chunks'] == 1
    assert overview['index_health']['healthy'] is True
    database.engine.dispose()


def test_mcp_discovery_contract(tmp_path):
    app = create_app(Settings(tmp_path / "contract.db"))
    def payload(result):
        return result.structured_content or json.loads(result.content[0].text)
    async def run():
        async with Client(app.state.mcp_server) as client:
            result = payload(await client.call_tool("edit_device", {"action":"discover", "request":"虚拟网线"}))
            assert len(result["capabilities"]) == 3
            result = payload(await client.call_tool("edit_device", {"action":"discover", "request":"unavailable capability"}))
            assert result["available_operations"]
            result = payload(await client.call_tool("query_device", {"action":"devices"}))
            assert result == {"ok":True, "items":[]}
            result = await client.call_tool("edit_device", {"action":"plan", "device_id":"missing", "changes":[{"operation_id":"interface.update", "payload":{}}]})
            assert result.is_error
    with TestClient(app):
        asyncio.run(run())
