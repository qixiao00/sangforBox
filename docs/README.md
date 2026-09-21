# docs 目录索引

本目录存放 SangforBox 的设计契约、操作指南与历史材料。**改动文档前先读本页**，按标签判断该文档的权威级别。

## 标签含义

| 标签 | 含义 | 能否直接引用为事实 |
| --- | --- | --- |
| 【现状】 | 已实现、与代码一致的契约 | ✅ 可以，但以代码为准 |
| 【目标】 | 已确认设计但尚未实现 | ⚠️ 可以引用为"设计要求"，不可当作已交付能力 |
| 【归档】 | 历史材料，不再维护 | ❌ 仅作背景阅读 |

---

## 文档清单

| 文档 | 标签 | 职责 | 读者 |
| --- | --- | --- | --- |
| [application-control.md](application-control.md) | 【现状】+【目标】 | 应用控制策略梳理的**唯一事实来源**：目标范围、主流程与状态机、统一语义模型、等价整理与意图修复、7 类优化能力、严格验证契约、MCP 工具契约、审核输出契约、LLM 与程序分工、验收场景、未实现清单、已实现模块索引 | 开发者、架构评审 |
| [DEVICE_EXECUTION_BOUNDARY.md](DEVICE_EXECUTION_BOUNDARY.md) | 【目标】 | 生产环境中强制所有设备操作经过 MCP 的隔离方案 | 部署者 |
| [guides/application-control-engineer-manual.md](guides/application-control-engineer-manual.md) | 【现状】 | 现场工程师操作手册：开始前准备、获取建议、手动实施、提交负反馈、验收检查 | 现场工程师 |
| [archive/knowledge-base-handoff-prompt.md](archive/knowledge-base-handoff-prompt.md) | 【归档】 | 可直接交给其他 AI Harness 的 PDF 数据加工任务书 | — |
| [archive/rag-learning-roadmap.md](archive/rag-learning-roadmap.md) | 【归档】 | 分阶段 RAG 技术栈与验收方法 | — |
| `device-page-preview.png` / `knowledge-page-preview.png` | 【归档】 | 早期页面截图 | — |

---

## 命名规范

新增文档时遵循下表，避免出现"设计/工作流/契约"各写一份、互相矛盾的情况。

| 文档类型 | 命名模式 | 示例 |
| --- | --- | --- |
| 领域主文档（目标+现状合一） | `<领域>.md` | `application-control.md` |
| 操作指南 | `guides/<领域>-<角色>-manual.md` | `guides/application-control-engineer-manual.md` |
| 架构边界/强制约束 | `<约束名>.md`（全大写下划线） | `DEVICE_EXECUTION_BOUNDARY.md` |
| 历史材料 | `archive/<原名>.md` | `archive/rag-learning-roadmap.md` |

**规则：**

1. **一个领域只允许一份主文档。** 设计与工作流内容合并进同一份，用 `【现状】`/`【目标】` 标签区分，不再拆成 `-design.md` 与 `-workflow.md`。
2. 操作指南放 `guides/`，不放在 `docs/` 根目录。
3. 归档文档不再修改，需要新内容请在主文档中重写。
4. 主文档必须在开头带状态表，明确哪些能力已实现、哪些没有。
5. 文档内引用其他文档用相对路径，保证在 git 托管环境（如 GitHub）可直接跳转。

---

## 相关目录

| 路径 | 内容 |
| --- | --- |
| [../README.md](../README.md) | 项目总览：启动、请求路径、设备 API、知识库、便携包 |
| [../connector/workbuddy/skills/ROUTING.md](../connector/workbuddy/skills/ROUTING.md) | Skill 路由唯一事实来源 |
| [../connector/workbuddy/skills/_SCHEMA.md](../connector/workbuddy/skills/_SCHEMA.md) | Skill frontmatter 字段规范 |
| [../connector/workbuddy/SKILL.md](../connector/workbuddy/SKILL.md) | 连接器总索引 |
