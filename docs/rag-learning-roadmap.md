# SangforBox RAG 学习路线

## 推荐技术栈

| 层 | 选择 | 在本项目中的作用 |
| --- | --- | --- |
| 文档解析 | Docling；简单文本 PDF 可先用 PyMuPDF | 保留标题、页码、表格和代码块 |
| 数据契约 | JSONL + JSON Schema | 让解析、索引、检索彼此解耦并可验证 |
| 编排 | 先写普通 Python 服务；需要快速实验时使用 LlamaIndex | 看清 RAG 每一步，避免框架隐藏检索逻辑 |
| Dense embedding | 外部 OpenAI-compatible Embeddings API | 不运行本地 embedding 模型 |
| 关键词检索 | SQLite FTS5 起步 | 先掌握 BM25 和精确参数名/API 路径检索 |
| 向量检索 | Qdrant Local 或 Docker | 保存向量、元数据、过滤条件，之后升级混合检索 |
| 融合 | Dense + BM25，以 RRF 合并 | 同时处理自然语言和精确命令/API 名 |
| Rerank | 外部 rerank API，可在第二阶段加入 | 对 Top 20～50 候选重新排序 |
| 生成 | 任意外部聊天模型 API | 只接收命中的少量片段，并输出页码引用 |
| API/MCP | 现有 FastAPI + FastMCP | Web、WorkBuddy 和其他 Harness 复用同一检索能力 |
| 评估 | 自建 `qa_seed.jsonl` + Precision@K、MRR、Recall@K | 先评检索，再评回答，定位问题更直接 |

## 为什么从 SQLite FTS5 + Qdrant 开始

AF 文档同时存在两类问题：

- “什么是应用控制策略”依赖语义相似度；
- `/api/path`、`policy_id`、错误码和 CLI 命令依赖精确词匹配。

只做向量检索容易漏掉精确技术符号。先用 SQLite FTS5 建立 BM25 基线，再增加 Qdrant dense 检索并用 RRF 融合，可以直观看到每一层带来的变化。Qdrant 支持元数据过滤、dense/sparse 多向量和混合查询，后续无需重写数据层。

## 四个学习阶段

### 1. 可审计数据

先完成 PDF → Markdown/JSONL。每个 chunk 保存 `doc_id`、标题路径、页码、版本和哈希。此阶段不生成向量。

验收：给定任意检索结果，可以立即定位原 PDF 页码并人工核对。

### 2. 关键词基线

把 `content_plain`、标题、关键词写入 SQLite FTS5，提供 `search_knowledge(query, filters, top_k)`。记录命中分数和来源。

验收：API 路径、参数名、CLI 命令和错误码问题能稳定命中。

### 3. Dense 与混合检索

调用外部 Embeddings API 批量生成向量，写入 Qdrant；查询时并行取 BM25 与 dense 候选，用 RRF 合并。embedding 模型名、维度和数据版本写入索引 manifest，换模型时建立新 collection。

验收：在固定评估集上比较 BM25、dense、hybrid 的 Recall@5 和 MRR，保留实验结果。

### 4. Rerank、生成和 MCP

对 hybrid Top 20 使用外部 rerank API，取 Top 5 交给外部大模型。提示词要求仅根据上下文回答，并返回文档名、页码、chunk ID。最后暴露：

- `search_knowledge`：只检索，便于调试；
- `answer_with_sources`：检索并生成答案；
- `get_source_chunk`：读取完整来源；
- `lookup_api_endpoint`：按 method/path 精确查询；
- `lookup_cli_command`：按命令精确查询。

验收：分别度量检索命中、引用正确率、忠实度和拒答表现，不用“看起来回答得不错”代替评估。

## 数据和隐私边界

- PDF、解析结果、Qdrant 和 SQLite 留在本地。
- 外部 Embeddings API 会看到 chunk 文本；外部聊天模型只看到最终 Top K 片段。
- 设备配置、密码、Token 和实时日志不要写入知识库，也不要发给外部模型。
- 若文档不能发送到外部 embedding 服务，就必须改用获批的企业 API 或本地 embedding 服务；这一边界无法通过向量数据库规避。
