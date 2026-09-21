---
name: af-device-initialization
description: 在 WorkBuddy 中引导工程师逐步完成深信服 AF 8.0.107 初始化设计，通过 SangforBox MCP 分阶段实施、验证并形成可评价的试用案例。
agent_created: true
layer: scenario
version: 1.0.0
---

# AF 初始化试用流程

只使用 `af-workbench` MCP。设备选择、查询、凭据、计划、执行和回读均由 MCP 完成。不得读取凭据库、导入设备客户端、拼接设备 URL，或生成 Python、curl、PowerShell 等直连脚本。MCP 不可用时说明阻塞并等待恢复。

## 对话收集设计

1. 用 `query_device(action="devices")` 选择设备，调用 `initialize_device(action="design", design={})` 建立空案例并取得 `case_id`，再用 `initialize_device(action="inspect", case_id=...)` 读取接口、区域、虚拟网线、网络对象、路由、NAT、安全防护、应用控制及本机访问控制。结果失败或截断时停止规划；可以用 `query_device` 精确排查，但本版初始化计划仍要求完整盘点。
2. 工程师可一次提交文字、接口表、拓扑图，也可逐轮回答。每轮把已确认的信息通过 `initialize_device(action="design", design=..., case_id=...)` 保存到同一案例。根据 `missing_context` 每轮只问当前最关键的少量问题，已回答的不重复问。图中看不清、文字与设备冲突的地方标为待确认。
3. 确认部署模式、AF 在拓扑中的位置、接口与 IP、内外方向、区域、业务网段、保护范围、路由、已有配置保留方式，以及 NAT、安全防护、应用控制的默认选择。`open_questions` 非空时继续对话。不得按接口或区域名称猜方向。
4. 汇总工程师要求、设备现状、手册依据和试用默认建议，逐项标明来源。工程师确认整份设计后调用 `initialize_device(action="design", case_id=..., confirm_design=true)`。设计变更会增加版本并使旧计划失效。

## 默认建议的边界

- 路由：对需要经 AF 三层转发的出口检查现有和设计中的 `0.0.0.0/0`、下一跳、出接口及可能的自动或动态路由。缺少时提出补充建议，下一跳请工程师确定；不要把全 0 路由当成每种部署模式都必须新增的规则。
- NAT：工程师未描述时，根据明确的业务流量和拓扑提出是否需要 NAT。转换地址、方向和匹配范围不明确时提问，不填臆测地址。
- 安全防护：先确认“全拒绝”是未匹配业务流量拒绝、威胁检测阻断，还是两者；再提出具体策略和启用状态。不能把两种含义混用。
- 应用控制：本版默认建议至少配置一条面向已确认业务网段和区域的放通规则，核对它相对设备自带拒绝规则的顺序、匹配范围和启用状态。不得生成无边界的全局放通。

默认建议也要出现在设计表中，标明“试用建议”，由工程师确认后进入计划。现有对象和策略先盘点重名、冲突、引用与依赖；需要更新时改用对应 PATCH/PUT 端点。未注册的手册接口使用知识库返回的 `endpoint_id`，不能因为没有 `operation_id` 就改写直连脚本。

## 分阶段实施

阶段依次考虑 `management`、`topology`、`objects`、`routing`、`nat`、`security`、`application`，跳过经工程师确认无需变更的阶段。每项先 `inspect(stage=...)`，再 `prepare(stage=..., changes=[...])` 获取必答问题。把用户答案写入 `decisions`，用 `plan(stage=..., case_id=..., changes=[单项变更])` 生成计划。展示目标、现状、影响、依赖、验证标准和回滚动作；工程师确认该计划后，使用同一 `plan_id` 执行 `apply`，再执行 `verify`。

管理口变更先确认备用管理路径、可接受的中断窗口和恢复办法。上一项结果未知或回读无法证明生效时暂停；工程师核查后，可用 `resolve` 记录明确的人工处理结论和原因。回读配置一致不等于业务连通性已通过；实际业务验证由工程师另行记录。

## 收集反馈

工程师完成现场调整后，转入 `af-initialization-feedback` Skill（见 [`ROUTING.md`](ROUTING.md)），用 `feedback_review(case_id=...)` 回读设备并对比实施前、MCP 实施后和当前配置。让工程师先确认差异，再填写每项满意度。反馈表、CSV 和事件日志均从同一案例生成。只把已确认的现场结论作为案例证据；单次评价不直接改写通用默认规则。

输出只保留设计待确认项、当前阶段、变更结果、未验证事项和下一步。详细端点、原始 JSON、内部 ID 仅在排障或工程师要求时展示。
