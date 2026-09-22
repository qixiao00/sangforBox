# AF Workbench / 框架交接

Windows 上运行的 Next.js Web + FastAPI + MCP Server + SQLite 本地知识库。
当前系统支持本地 SQLite 全文检索、API 端点查询、CLI 命令查询、AF 设备查询及通用配置计划。应用控制策略梳理已支持只读分析、CSV 审核、组合复验、只读预检及工程师手动变更后的差异日志；优化方案的专用执行闭环尚未接入。向量 Embedding 和外部大模型问答也尚未接入。

## 启动

需要 Python 3.11+（本项目使用 Windows Python 3.12 验证）。仓库已包含 Next.js 的静态构建结果，运行服务不需要 Node.js，也不需要模型。

```powershell
.\setup.ps1
.\start.ps1
```

访问 http://127.0.0.1:8765 ，直接运行测试。
此命令以前台进程运行，Ctrl+C 停止。首次启动只建立 SQLite 基础版本表。

### 运行态目录

运行数据存放在**当前 Windows 用户的应用数据目录**，与源码分离，源码目录保持干净：

```text
%LOCALAPPDATA%\SangforBox\
  af.db             设备档案、知识库索引、初始化案例
  policy-review.db  应用控制审核记录
  results\          审核 CSV、发现项 CSV、反馈 JSON
  logs\             启动与访问日志
```

覆盖方式（按优先级）：

| 环境变量 | 作用 | 默认值 |
| --- | --- | --- |
| `AF_STATE_DIR` | 整体改运行态根目录 | `%LOCALAPPDATA%\SangforBox` |
| `AF_DATABASE_PATH` | 只改主数据库位置 | `<状态目录>\af.db` |
| `AF_OUTPUT_DIR` | 只改导出目录 | `<状态目录>\results` |

非 Windows 平台回退到 `$XDG_STATE_HOME/SangforBox` 或 `~/.local/state/SangforBox`。

服务当前不包含应用层鉴权，默认只监听 `127.0.0.1`。若绑定到局域网或公网，应在可信反向代理上配置身份认证和 HTTPS。

## 请求路径

```text
浏览器 → POST /api/test → 进程内 MCP Client → framework_probe → ProbeService → SQLite
WorkBuddy → /mcp/ (Streamable HTTP) → framework_probe → ProbeService → SQLite
```

`POST /api/test` 接受 `{"message":"hello"}`，返回真实数据库连接检查。
`POST /api/test?stream=true` 使用相同处理流程，以 NDJSON 推送工作事件。
MCP 使用官方 Python SDK 2.2.0；框架测试和设备连接测试均通过进程内 MCP 调用。

## AF 设备 API 连接

设备账户需要先在 AF WebUI 的“系统 → 管理员账号”中启用 **WEB API** 权限。连接层按 AF 8.0.107 手册执行以下流程：

1. `POST /api/v1/namespaces/{namespace}/login`；
2. 读取 `data.loginResult.token`；
3. 后续请求携带 `Cookie: token=<token>`；
4. 连接测试调用 `GET .../keepalive`；
5. 测试结束后调用 `POST .../logout`。

设备密码使用 Windows DPAPI 加密后存入 SQLite，只能由保存密码的 Windows 用户解密。列表、详情、日志和 MCP 返回中均不包含密码或密文。

Web 左侧“设备”入口提供设备添加、编辑、删除、连接测试和审计记录界面，也可直接访问 `http://127.0.0.1:8765/#devices`。

HTTP 接口：

```text
GET    /api/devices
POST   /api/devices
GET    /api/devices/{device_id}
PATCH  /api/devices/{device_id}
DELETE /api/devices/{device_id}
POST   /api/devices/{device_id}/test
GET    /api/devices/{device_id}/audits
```

创建示例：

```powershell
$device = Invoke-RestMethod http://127.0.0.1:8765/api/devices `
  -Method Post -ContentType 'application/json' `
  -Body '{"name":"AF-Lab","base_url":"https://192.168.1.1","namespace":"public","username":"api-admin","password":"替换为真实密码","verify_tls":false}'

Invoke-RestMethod "http://127.0.0.1:8765/api/devices/$($device.device_id)/test" -Method Post
```

`verify_tls`默认启用。使用自签名证书时，优先通过 `ca_bundle`指定 CA 文件；仅在受控测试环境中关闭校验。Web 的连接测试会通过进程内 MCP 调用同名工具，统一经过 MCP 执行链路。每次测试会记录成功状态、阶段、耗时和脱敏消息，不记录密码或 token。

## 本地知识库

WorkBuddy 加工后的 JSONL 位于 `knowledge/processed/`。导入或重建数据库索引：

```powershell
.\import-knowledge.ps1
```

该命令会完整重建 SQLite 中的文档、chunk、API、CLI 和 FTS5 索引。JSONL 始终是可审计的数据源，`af.db` 是可重新生成的运行数据库（位于运行态目录，见上）。

当前检索采用 jieba 中文分词 + SQLite FTS5 BM25，并对标题和关键词提高权重。检索层保留原始 chunk，不改写 PDF 内容；返回结果会折叠完全重复的正文，并包含 PDF 页码与相邻 chunk ID。

HTTP 接口：

```text
GET  /api/knowledge/stats
GET  /api/knowledge/overview
POST /api/knowledge/search
GET  /api/knowledge/chunks/{chunk_id}
GET  /api/knowledge/api-endpoints?path=...&method=GET
GET  /api/knowledge/cli-commands?q=...
```

检索示例：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/knowledge/search `
  -Method Post -ContentType 'application/json' `
  -Body '{"query":"如何获取应用控制策略列表","top_k":5}'
```

Web 左侧“知识库”入口展示输入文档、分页与切分统计、API/CLI 结构化抽取数量、FTS5 覆盖率、索引流水线和检索抽查结果，也可直接访问 `http://127.0.0.1:8765/#knowledge`。

MCP 工具：

- `search_knowledge`：检索本地手册并返回页码；
- `get_source_chunk`：按 ID 读取完整原文；
- `lookup_api_endpoint`：按自然语言、路径和方法查询结构化 API 摘要；
- `get_api_endpoint`：按 endpoint_id 分页读取请求字段、响应字段或样例；
- `lookup_cli_command`：查询 CLI 语法；
- `query_device`：优先使用已审核的只读能力；未注册的 GET 可用知识库 `endpoint_id` 通用执行；
- `edit_device`：先用 `prepare` 获取新增/修改、同名对象处理和策略启用状态等必答问题；答案完整后才生成持久化计划，确认后按 plan_id 执行；
- `initialize_device`：多轮保存初始化设计、分阶段检查与执行、回读验证、现场差异反馈和案例事件；
- `framework_probe`：检查框架和数据库状态。

工程师试用流程、反馈 CSV 与日志说明见 [AF 初始化工程师指南](connector/workbuddy/docs/af-initialization-engineer-guide.md)。

## 前端开发

页面源码位于 `frontend/`，使用 Next.js App Router、React 和 TypeScript。FastAPI 托管导出的静态页面，因此生产和离线环境仍只需启动一个 8765 服务。

修改页面后执行：

```powershell
.\build-web.ps1
```

该脚本安装锁定的 pnpm 依赖、运行 Next.js 类型检查和生产构建，并将导出结果同步到 `af_app/static/`。需要联调界面时也可以在 `frontend/` 运行 `pnpm dev`；Next.js 开发服务器需自行代理 `/api` 到 8765，日常验收建议直接使用上述静态构建。

## 工作日志 / Agent 扩展

每次 Web 请求生成独立 request_id。事件包括 actor、actor_type、parent_id、status、message、timestamp、elapsed_ms。
当前 actor 是 Web / MCP / SQLite，全部是普通模块，没有 AI 子 Agent。
日志来自实际执行点，没有定时器模拟进度。测试很快，可能一瞬间全部完成。
前端按流更新状态；支持失败和取消等待。请求之间的队列隔离，并在结束后释放。
当前日志仅保留当前页面请求，不持久化；WorkBuddy 内部 Agent 状态也无法由本服务获取。
后续 Agent 在 `events.py` 基础上扩展 actor_type 和 parent_id；再增加历史表及订阅接口。

## WorkBuddy 连接

`connector/workbuddy/` 提供 MCP + Skill 连接器，默认连接本机 `http://127.0.0.1:8765/mcp/`，无需填写令牌。启动 SangforBox 后，在 WorkBuddy 中导入该连接器目录（或压缩包），连接成功后即可使用。

若使用 WorkBuddy 的自定义 MCP 表单，选择 Streamable HTTP，地址填写 `http://127.0.0.1:8765/mcp/`，不配置请求头。连接后可使用知识库、设备业务工具和应用控制策略梳理工具。完整连接器包包含独立的 `af-app-control-optimization` Skill，会将“梳理、命名、遮蔽、合并、对象化、顺序分析”路由到分析与审核流程；用户明确指定的单条策略配置仍使用 `af-app-control-policy`。

只读验收提示词：“梳理 AF-Lab 的应用控制策略，先给我审核 CSV 和问题 CSV。不要修改设备。”预期调用 `analyze_app_control` → `get_app_control_analysis`；用户逐项决定后才调用 `review_app_control` → `verify_app_control_review`，需要核对实时配置时调用只读 `preflight_app_control_review`。工程师按工单手动修改后，调用 `record_app_control_feedback` 生成可下载的 JSON 差异日志，并用 `list_app_control_feedback` 找回历史日志。工程师操作及反馈交接参见 [应用控制策略梳理工程师手册](docs/guides/application-control-engineer-manual.md)。优化执行适配尚未完成，不得把审核结果交给通用 `edit_device` 下发。

远程部署需由 HTTPS 反向代理转发到本机 8765，保留 MCP 请求头和尾部斜杠，并在代理层增加访问控制。
设置 `AF_ALLOWED_HOSTS` 为允许的主机名（逗号分隔，如 `af-kb.company.local,127.0.0.1:*`），使 MCP Host 校验接受部署域名。
使用工作日志时关闭代理响应缓冲。MCP 客户端所在环境必须能访问该地址；云端执行环境无法直接访问内网地址。
默认监听回环地址。此骨架不包含生产证书、域名、OAuth 或 Windows Service 安装器。

连接器配置和 MCP 协议由本地测试验证；WorkBuddy 客户端中的实际导入/握手需在有客户端及可达 HTTPS 地址后联调，不能把配置准备等同于已经连接。
官方格式参考：https://open.workbuddy.cn/docs/connector

## Windows 便携包

在开发机安装依赖并通过测试后运行：

```powershell
.\package-portable.ps1
```

默认输出到项目同级的 `SangforBox-Portable` 目录，并同时生成 `SangforBox-Portable.zip`。也可用 `-OutputDirectory` 指定另一个专用目录。目标 Windows 电脑无需安装 Python 或项目依赖；解压整个目录后双击 `SangforBox.exe`，在 WorkBuddy 的“连接器”入口导入 `WorkBuddy\sangforbox-workbuddy-connector.zip`；若使用“技能”入口，把 `WorkBuddy\` 下的全部 `*-skill.zip` 逐个导入，每个 ZIP 的根目录直接包含该 Skill 的 `SKILL.md`。

EXE 首次启动时在**当前用户的 `%LOCALAPPDATA%\SangforBox\`** 生成 `af.db`（从内置的无设备档案知识库种子），并创建 `results` 和 `logs`。因此更新便携包时可以直接替换整个安装目录，设备档案、审核记录和导出结果都不会丢失。网页下载的审核 CSV、发现项 CSV和反馈 JSON 会保存到该目录的 `results` 子目录。服务仅监听 `127.0.0.1:8765`；设备密码使用目标电脑当前 Windows 用户的 DPAPI 加密。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 知识库交接与 RAG 学习（归档）

- `docs/archive/knowledge-base-handoff-prompt.md`：可直接交给其他 AI Harness 的 PDF 数据加工任务书。
- `docs/archive/rag-learning-roadmap.md`：本项目的分阶段 RAG 技术栈与验收方法。

两份均属历史材料，不再维护；文档索引与标签说明见 [docs/README.md](docs/README.md)。

知识库加工先输出带来源页码的 JSONL，不直接生成向量。SangforBox 后续负责 embedding、索引、检索、生成和 MCP 工具层，便于更换模型并独立评估每一步。

## 后续模型从哪里继续

| 位置 | 职责 |
| --- | --- |
| af_app/app.py | FastAPI、鉴权、MCP 挂载、流式测试 API |
| af_app/mcp_server.py | MCP 工具定义 |
| af_app/service.py | 普通 Python 业务服务 |
| af_app/devices.py | AF API 客户端、DPAPI 凭据、设备查询与计划执行 |
| af_app/operations.py | 审核能力注册表、文档端点动态解析、参数校验和持久化计划 |
| connector/workbuddy/skills | 随连接器分发的 AF 场景 Skill 集合 |
| connector/workbuddy/skill-template | 测试能力沉淀为 Skill 的模板 |
| docs/application-control.md | 应用控制策略梳理：设计契约、状态机、工具契约、已实现模块索引 |
| docs/README.md | 文档索引、标签说明与命名规范 |
| docs/DEVICE_EXECUTION_BOUNDARY.md | 生产环境中强制所有设备操作经过 MCP 的隔离方案 |
| af_app/database.py | SQLAlchemy 基础表与数据库生命周期 |
| af_app/events.py | 请求工作日志事件契约 |
| frontend/ | Next.js、React、TypeScript 前端源码 |
| af_app/static/ | Next.js 静态导出产物，由 FastAPI 托管 |
| connector/workbuddy/ | WorkBuddy 连接器配置与工具使用说明 |

应用控制策略模块：

| 位置 | 职责 |
| --- | --- |
| af_app/policy_optimizer.py | 应用控制审核服务；异步分析任务、中文审核 CSV 导出/读取 |
| af_app/policy_snapshot.py | 完整分页采集策略/对象/策略组，校验分页完整性与两次读取一致性 |
| af_app/policy_semantics.py | 保守的 AF 网络对象语义与地址空间分析（仅静态 IP/ADDRGROUP） |
| af_app/policy_sets.py | 精确区间、多维集合差、联合遮蔽有效区域及首条匹配等价证明 |
| af_app/policy_combination.py | 重建拟执行配置并做保守首条匹配模型组合验证 |
| af_app/policy_plan_validation.py | 逐个中间状态的等价验证、快照与状态连续性检查 |
| af_app/policy_review.py | 不可变分析/审核记录、指纹绑定、依赖互斥校验、幂等提交 |
| af_app/policy_feedback.py | 工程师手动变更后的只读重采集与结构化差异日志 |
| af_app/app_control_analyzer.py | 离线分析入口：读策略/对象 CSV，输出遮蔽、重复、合并、对象化、命名候选 |

契约细节见 [docs/application-control.md](docs/application-control.md)；文档索引见 [docs/README.md](docs/README.md)。

添加真实业务时，先补数据库迁移和服务，再添加 MCP 工具，Web 通过 MCP 使用它。
前端可独立扩展动画、Agent 执行轨迹和业务视图，不改变 MCP 入口。

## 应用控制策略梳理分析器（离线）

离线分析入口位于 `af_app/app_control_analyzer.py`。它读取应用控制策略和网络对象 CSV，生成策略遮蔽、重复、合并候选、对象复用候选、命名候选和审核汇总；当前不会连接设备或下发配置。

```powershell
python -m af_app.app_control_analyzer `
  --policy D:\chrome_downloads\appcontrol_1789703366048.csv `
  --objects D:\chrome_downloads\ipgroup.csv `
  --out .\reports\app-control
```

设备读取和后续变更必须通过现有 SangforBox MCP 的应用控制能力及 `prepare → plan → apply → verify` 流程接入。分析器不会建立第二套设备调用链。
