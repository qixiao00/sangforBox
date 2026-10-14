---
name: af-device-initialization
description: 在 WorkBuddy 中引导工程师逐步完成深信服 AF 8.0.107 初始化设计，通过 SangforBox MCP 分阶段实施、验证并形成可评价的试用案例。
agent_created: true
layer: scenario
version: 1.1.1
---

# AF 初始化试用流程

只使用 `af-workbench` MCP。设备选择、查询、凭据、计划、执行和回读均由 MCP 完成。不得读取凭据库、导入设备客户端、拼接设备 URL，或生成 Python、curl、PowerShell 等直连脚本。MCP 不可用时说明阻塞并等待恢复。

## 对话收集设计

1. 用 `query_device(action="devices")` 选择设备，调用 `initialize_device(action="design", design={})` 建立空案例并取得 `case_id`，再用 `initialize_device(action="inspect", case_id=...)` 读取接口、区域、虚拟网线、网络对象、路由、NAT、安全防护、应用控制及本机访问控制。结果失败或截断时停止规划；可以用 `query_device` 精确排查，但本版初始化计划仍要求完整盘点。
2. 工程师可一次提交文字、接口表、拓扑图，也可逐轮回答。每轮只把工程师明确确认的信息通过 `initialize_device(action="design", design=..., case_id=...)` 保存到同一案例。设备现状只能作为“当前配置”证据，不得用它替用户决定目标拓扑、接口用途、保护范围或默认选择。根据 `missing_context` 每轮只问当前最关键的少量问题，已回答的不重复问。图中看不清、文字与设备冲突的地方放入 `open_questions`。
3. 确认部署模式、AF 在拓扑中的位置、接口与 IP、内外方向、区域、业务网段、保护范围、路由、已有配置保留方式，以及 NAT、安全防护、应用控制的默认选择。`open_questions` 非空时继续对话。不得按接口或区域名称猜方向。
4. 设计必填字段是 `deployment_mode`、`topology`、`interfaces`、`zones`、`routing`、`preserve_existing`、`business_subnets`、`protection_scope`、`default_choices`。允许的可选字段还有 `address_plan`、`nat`、`security`、`application`、`management_recovery`、`open_questions`、`notes`，不要自造字段名。
5. `default_choices` 必须是嵌套对象，并分别包含非空的 `nat`、`security`、`application`，例如 `{"nat":"本阶段不配置","security":"先生成建议并逐项确认","application":"先生成建议并逐项确认"}`。这些值是工程师的设计决定，不得根据设备现状代填。
6. 汇总工程师要求、设备现状、手册依据和试用建议，逐项标明来源。`missing_context` 和 `open_questions` 清空后，先完整展示设计及当前 revision，请工程师明确确认。收到确认后调用 `initialize_device(action="design", case_id=..., design={}, confirm_design=true)`。确认设计只锁定当前版本，**不会生成计划，也不会写设备**；设计变更会增加版本并使旧计划失效。

## 默认建议的边界

- 路由：对需要经 AF 三层转发的出口检查现有和设计中的 `0.0.0.0/0`、下一跳、出接口及可能的自动或动态路由。缺少时提出补充建议，下一跳请工程师确定；不要把全 0 路由当成每种部署模式都必须新增的规则。
- NAT：工程师未描述时，根据明确的业务流量和拓扑提出是否需要 NAT。转换地址、方向和匹配范围不明确时提问，不填臆测地址。
- 安全防护：先确认“全拒绝”是未匹配业务流量拒绝、威胁检测阻断，还是两者；再提出具体策略和启用状态。不能把两种含义混用。
- 应用控制：本版默认建议至少配置一条面向已确认业务网段和区域的放通规则，核对它相对设备自带拒绝规则的顺序、匹配范围和启用状态。不得生成无边界的全局放通。

默认建议也要出现在设计表中，标明“试用建议”，由工程师确认后进入计划。现有对象和策略先盘点重名、冲突、引用与依赖；需要更新时改用对应 PATCH/PUT 端点。未注册的手册接口使用知识库返回的 `endpoint_id`，不能因为没有 `operation_id` 就改写直连脚本。

## 分阶段实施

阶段依次考虑 `management`、`topology`、`objects`、`routing`、`nat`、`security`、`application`，跳过经工程师确认无需变更的阶段。每项先 `inspect(stage=...)`，再 `prepare(stage=..., changes=[...])` 获取必答问题。`prepare` 的 `required_context` 必须原样向工程师确认，不能由模型替答。把答案写入 `decisions`，再调用 `plan(stage=..., case_id=..., changes=[单项变更])`；`stage` 是 plan 的必填参数。plan 只生成计划，不写设备。展示目标、现状、影响、依赖、验证标准和回滚动作；工程师明确确认该 `plan_id` 后才能执行 `apply`，随后执行 `verify`。

接口属于虚拟网线组或区域时，把关联接口、虚拟网线组和区域视为同一个拓扑依赖集。不得根据 inspect 结果自行决定解绑、移动或删除顺序；先由 `prepare` 返回必答项，再让工程师确认完整影响和恢复路径。

当前 Web API 地址所属接口是不可变的管理口：初始化流程不得修改该接口的 IP、模式、启停状态、名称、区域关系或其他配置，也不得删除、解绑或迁移该接口。即使工程师提供备用管理路径或维护窗口，也不能通过本初始化流程执行；如确需迁移管理口，必须退出当前案例，改由工程师在带外管理条件下手工实施并重新建立设备档案。SangforBox 在 plan 和 apply 前都会硬性校验；无法从完整接口盘点中可靠识别当前 Web API 地址所属接口时，所有初始化接口变更都必须停止。

上一项结果未知或回读无法证明生效时暂停；工程师核查后，可用 `resolve` 记录明确的人工处理结论和原因。回读配置一致不等于业务连通性已通过；实际业务验证由工程师另行记录。

## 收集反馈

工程师完成现场调整后，继续使用本 Skill，调用 `initialize_device(action="feedback_review", case_id=...)` 回读设备并对比实施前、MCP 实施后和当前配置。让工程师先确认差异，再填写每项满意度，并通过 `feedback_submit` 提交完成的 CSV。反馈表、CSV 和事件日志均从同一案例生成。只把已确认的现场结论作为案例证据；单次评价不直接改写通用默认规则。

输出只保留设计待确认项、当前阶段、变更结果、未验证事项和下一步。详细端点、原始 JSON、内部 ID 仅在排障或工程师要求时展示。
