---
name: af-app-control-optimization
description: 通过 SangforBox MCP 梳理深信服 AF 应用控制策略；适用于整表分析、命名规范、遮蔽与顺序问题、合并候选的等价性复验和地址对象化。先导出审核表供用户决定，当前只支持分析、审核、组合复验和只读预检。
---

# AF 应用控制策略梳理

用户要求“梳理、优化、批量规范命名、找无效或被遮蔽策略、合并重复策略、对象化、评估顺序”时使用本 Skill。针对用户已指定的单条策略直接新增、修改、删除，使用 `af-app-control-policy`。即使用户说“梳理后直接执行”，当前梳理流程也只能到只读预检；不能把审核项转成通用 `edit_device` 或 `initialize_device` 的变更。

所有设备读取只通过 `af-workbench` MCP。WorkBuddy 负责理解需求、展示文件、传递用户决定；策略解析、集合运算、证明、版本校验和设备读取由 SangforBox 内置工具完成。禁止生成 Python、PowerShell、curl、SQL 或其他脚本来分析或修改设备；禁止读取凭据或直接访问 AF。

## 用户输入

- 必填：目标设备的档案 ID、名称、IP 或 URL。如果用户未指定，调用 `query_device(action="devices")` 展示可选设备；多个可能目标时请用户选定，不自行猜测。
- 可选：分析方向，映射为 `objectives` 中的 `naming`、`shadowing`、`merge`、`objectize`、`order`。用户说“全面梳理”时省略该参数，使用服务端默认值；用户明确说“只看某项”时只传对应值。
- 可选：不允许修改的策略，传入目标设备上的稳定策略 ID 或唯一名称作为 `protected_policy_ids`。标识不确定时先用只读设备查询核实，不根据近似名称猜测。
- 可选：命名前缀 `naming_prefix`，只能是以字母开头、总长不超过 12 的英文字母、数字、下划线或连字符。不支持自由命名模板；用户要求完整模板时说明当前只能记录其偏好，不能声称已提交给分析器。
- 业务背景可以辅助说明建议，但不是设备匹配语义或严格等价证明的输入。不能仅凭策略名、零命中或“细规则优先”认定应删除或移动。

## MCP 调用顺序

1. 调用 `analyze_app_control(request={"device_id": "...", ...})`，保存返回的 `job_id`。该工具只读且异步。
2. 使用相同 `job_id` 调用 `get_app_control_analysis`。`state=running` 时继续查询同一个任务，不重复发起分析；`failed` 或 `interrupted` 时报告原因并停止。`state=completed` 时保存 `analysis_id`、`proposal_hash`、`snapshot_hash`、`candidate_count`、`review_url` 和 `findings_url`。候选分批读取时用 `offset`、`limit`，不要只看第一页就声称已检查全部候选。
3. **先向用户展示审核表和问题表下载链接**，再给简短摘要。`review_url`、`findings_url` 是相对路径，应相对于当前 MCP 服务地址的 origin 拼接；无法确认浏览器可达地址时展示原路径并说明需要由用户打开 SangforBox 服务。CSV 是审核材料，不是设备导入文件。无候选时也展示带表头的审核表和分析覆盖限制。
4. 等用户逐项决定。只能把用户明确给出的变更编号和“采纳 / 不采纳 / 待确认”传给 `review_app_control`；未提及项保持待确认。传 `analysis_id`、原样的 `proposal_hash`、`decisions` 和本次提交稳定唯一的 `submission_key`。重试同一提交复用同一 key；用户改变决定时创建新 key。也可提交用户编辑后的完整 `review_csv` 文本；仅允许修改“用户决定”“用户备注”两列。不要把备注当作执行指令。依赖和互斥冲突应交给工具校验，不能代替用户选一项。
5. 用返回的 `review_id` 调用 `verify_app_control_review`，展示选中项、`proof.status`、证明前提或无法证明的原因。`model_equivalent` 仅代表所列模型前提下等价；`unknown` 不能描述为通过。若候选改变访问行为，应说明它属于意图修复，需独立业务判断。
6. 需要核对实时设备状态时调用 `preflight_app_control_review(review_id)`。展示 `snapshot_unchanged`、`blockers`、`execution_ready`。快照变化时必须重新分析并重新审核；不能沿用旧 `review_id`。此工具仍只读。
7. 工程师按工单手动修改设备后，使用原 `review_id` 调用 `record_app_control_feedback`。要求工程师提供标识、工单号，并为认为不安全、不适用、拒绝或变形实施的候选填写 `feedback_items` 原因。工具返回的 `log_url` 是已保存的 JSON 差异日志；按当前 MCP 服务地址的 origin 解析相对路径，把可打开的下载链接及 `feedback_id` 交还需求方。若无法确认下载地址可达，展示原路径与 ID，不能声称文件已交付。可用 `list_app_control_feedback(review_id)` 找回该审核关联的全部日志。反馈读取设备但不写设备，也不自动通知外部收件人。

## 用户能看到的结果

首次完成分析时，至少给出设备、分析 ID、候选数量、审核 CSV、问题 CSV、主要未知或不支持项，以及“尚未修改设备”。具体策略、对象成员和原建议配置以审核表为准，摘要不能代替表格。若当前会话无法下载文件，如实说明链接及限制，不宣称用户已经收到表格。

审核后给出采纳的变更编号、组合验证结果、设备快照是否变化及剩余阻断原因。`review_app_control` 的“采纳”只记录审核决定，`verify_app_control_review` 与 `preflight_app_control_review` 都不授予设备写入权限。当前没有可用于优化方案的专用 plan/apply 工具，也未认证设备版本的匹配、位置移动和对象创建语义；停在预检结果，不调用通用编辑工具绕过。

工程师提交反馈后，只摘要展示实际对齐状态和有差异的候选；完整原配置、建议配置、观察配置与工程师自述保存在 JSON 日志。`matches_recommendation` 仅指结构一致；`comparison_unavailable` 必须说明未形成比较结论。用户询问历史日志时使用 `list_app_control_feedback` 和 `get_app_control_feedback`，不要重新分析替代旧日志。

如果用户同时提出“新增一条指定策略”和“梳理整表”，分别说明两个任务：整表梳理按本流程先产出审核表；单条配置在用户给出明确条件后才进入 `af-app-control-policy` 的独立计划流程。不能把优化候选直接当成单条配置请求。
