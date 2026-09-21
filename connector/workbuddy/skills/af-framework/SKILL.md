---
name: af-framework
description: 通过 SangforBox 查询、变更和初始化深信服 AF 8.0.107 设备，并按厂商运维口径解释实时配置、API 与手册信息；是所有 AF 场景的总入口与路由层。
agent_created: true
layer: framework
version: 1.0.0
---

# AF 设备操作

默认使用 `af-workbench` MCP 内部的 AF HTTP API。设备凭据、登录、Cookie 和注销由服务端处理。

## 应用控制请求路由

- 用户说“梳理、优化、批量规范命名、找重复/遮蔽/未生效策略、合并、对象化、分析顺序或例外关系”时，使用 `af-app-control-optimization`。先调用 `analyze_app_control`，把审核 CSV 和问题 CSV 交给用户，再按明确决定调用 `review_app_control`、`verify_app_control_review` 和只读 `preflight_app_control_review`。用户说“全部采纳”也不等于设备执行授权。
- 用户明确指定单条应用控制策略的新增、修改、删除和完整目标条件时，使用 `af-app-control-policy` 的独立配置流程。
- “调整顺序使其生效”或“按梳理建议执行”属于整表效果判断，进入优化 Skill。用户明确指定某条策略的目标位置且意图是直接配置时，才进入单条配置流程；不得拿优化审核结果填入通用编辑请求。
- 优化工具当前没有专用 plan/apply。模型等价和快照未变仍不是设备执行许可；在专用执行适配完成前停在只读预检。
- 工程师手动实施后的差异反馈使用 `record_app_control_feedback`，按 `review_id` 绑定原建议版本，并将可下载的 JSON 日志交还需求方。历史日志用 `list_app_control_feedback` 查找，不用新分析覆盖旧证据。

禁止为完成 AF 任务编写 Python、curl 或其他直连脚本，禁止读取凭据库，禁止修改本机 Skill，禁止自动改用 SSH/CLI。若出现 `Session not found`，说明 WorkBuddy 与 MCP 的会话已失效；提示用户重新连接连接器后重试，不绕过 MCP。

这条限制适用于所有设备业务操作，而不只适用于已注册能力。不得调用 Bash、Write、Edit 或本地 Python 来查询、试探或修改 AF；不得导入 `AFAPIClient`、`DeviceRepository` 或读取 `af.db` 中的凭据。MCP 暂时无法完成时应报告阻塞原因。

## 最短调用路径

- 查询实时配置：直接调用 `query_device(action="execute")`。`device_id` 可传设备名、IP、URL 或档案 ID。
- 选择设备：只有目标不明确时才调用 `query_device(action="devices")`。
- 未知能力：先调用一次对应工具的 `discover`。优先看 `capabilities`，没有合适的注册能力时查看 `documented_endpoints`；其中标为 `executable=true` 的端点可直接使用 `endpoint_id`。若仍需消歧，再调用 `lookup_api_endpoint` 和 `get_api_endpoint`。
- 参数不清楚：调用 `describe(operation_ids=[...])`，使用返回的请求定义和样例。
- 配置变更：`edit_device(prepare)` → 逐项向用户询问返回的必答问题 → `edit_device(plan)` → 向用户展示计划 → 用户确认 → `edit_device(apply)` → `query_device` 验证。
- 应用控制整表梳理：遵循 `af-app-control-optimization` 的分析、导出、审核、复验和只读预检流程；本通用配置变更路径不接收梳理候选。
- 设备初始化：按 `af-device-initialization` Skill，通过 `initialize_device` 保存并确认设计版本，再按 `inspect → prepare → plan → 用户确认 → apply → verify` 分阶段执行；现场调整后按 `af-initialization-feedback` Skill 回读差异并提交评价。不得用初始化工具查设备 ID 或测试连接。
- 已注册接口使用 `operation_id`；未注册接口使用 `endpoint_id`。二者只填一个。
- 通用 GET：`query_device(action="execute", endpoint_ids=[...], path_parameters=..., query_parameters=...)`。
- 通用修改：把 `endpoint_id`、`path_parameters`、`query_parameters`、`body` 放入 `edit_device(action="plan")` 的 change。请求体字段放在 `body`，不得使用 `payload` 或擅自包裹 `obj`。
- `prepare` 返回的问题必须由用户选择，不得根据经验替用户决定。典型问题包括：新增还是修改已有配置、同名对象存在时如何处理、新增策略是否立即启用。
- 将答案写入 change 的 `decisions.on_existing` 和 `decisions.initial_enabled`。选择“修改已有”时改用对应 PUT/PATCH 接口，不能继续调用 POST。

普通设备查询无需先调用 `framework_probe`，无需先查询产品手册。用户问“当前配置、当前状态、是否存在”时以设备实时数据为准；用户问“怎么配置、字段含义、版本支持”时才使用知识库工具。不要把手册定义当成设备现状。

知识库中的 API 端点也是通用执行目录。WorkBuddy 负责理解自然语言并选择 `operation_id` 或唯一 `endpoint_id`；MCP 从本地索引读取固定的 method、path 和参数定义，校验后通过 AF API 执行。WorkBuddy 不自行拼 URL，也不写脚本绕过。若检索到多个相似端点，先按查询/新增/修改/删除语义和资源层级消歧，无法唯一确定时向用户说明候选项。

`operation_id` 缺失不代表接口不可执行；它只是没有业务快捷名称。只要 `endpoint_id` 来自本 MCP 的知识库，就继续走同一个 `prepare → plan → apply` 流程。

通用执行边界：

- `GET` 只能通过 `query_device` 执行。
- `POST`、`PUT`、`PATCH`、`DELETE` 只能通过 `edit_device` 或 `initialize_device` 的 `plan → apply` 执行。
- MCP 只接受索引中真实存在的 `endpoint_id`，自动替换当前设备的 host 与 namespace，并拒绝未记录的查询参数、缺失的路径参数、`_method` 覆盖及任意 URL。
- 修改端点若存在同路径 GET，MCP 在生成计划前自动读取当前状态；执行后仍需回读验证。

## AF 判读原则

- 使用 AF 产品术语：接口、区域、虚拟网线组、网络对象、安全策略、NAT 策略、应用控制策略。
- 只根据返回字段下结论。方向、业务用途、链路角色无法从配置证明时，标注“待确认”，不要按名称推断。
- 策略类配置同时检查启用状态、引用对象、源/目的区域、动作和顺序。创建成功不代表生效；验证必须读取实际配置。
- 接口或区域变更应说明可能影响的转发类型和策略匹配。删除、模式切换、策略放行/拒绝等变更必须提供回滚信息。
- 设备返回与手册不一致时，明确写“设备实测”；不要推广为所有版本的通用规则。

## 对用户的输出

先用一句话给出结论，然后只展示与需求相关的数据。

查询类建议格式：

```text
结论：当前已配置 1 组虚拟网线，eth3/eth4 分别属于对应的 VIRTUAL 区域，配置关系完整。

| 对象 | 当前配置 | 状态 |
| ...  | ...      | ...  |

异常/待确认：仅在确有异常或无法判断时列出。
```

变更类建议格式：

```text
结果：配置已完成并通过回读验证。
- 变更对象：...
- 实际变化：...
- 业务影响：...
- 回滚方式：...
```

遵守以下表达规则：

- 查询成功通常控制在一张小表或 3–6 个要点。
- 只列相关对象；例如查询虚拟网线时，不展开全部普通区域。
- 不展示 PID、Session ID、Token、工具调用次数、完整 URL 请求、原始 JSON、临时文件和调试过程。
- 不叙述“我先查了什么、又试了什么”，除非失败原因直接影响用户下一步。
- 不使用“好消息”“踩坑”“设备怼回来”等口语化判断；使用“结果、原因、影响、建议”。
- operation_id、endpoint_id 和 UUID 仅在排障、审计或用户明确要求时展示。
- 不主动生成或展示日志文件、参数文件和探针脚本。

最终答案必须区分：`设备实时数据`、`手册定义`、`分析判断`。没有异常时无需制造提醒。
