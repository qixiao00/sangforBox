# 应用控制策略梳理：工程师使用手册

适用范围：SangforBox 当前的应用控制策略分析、审核、工程师手动变更后的反馈。优化工具会读取设备配置，但尚不自动下发优化方案。审核 CSV 是决策材料，不是设备导入文件。

## 一、开始前

1. 在 SangforBox 的“设备”页面登记目标 AF，使用具备所需查询权限的 API 账号，验证连接。生产环境优先使用可信 CA 验证 TLS。
2. 在 WorkBuddy 导入最新版 SangforBox 连接器，并确认 MCP 地址能访问。应用控制整表梳理应由 `af-app-control-optimization` Skill 处理。
3. 为本次作业准备变更工单号、工程师标识、维护窗口、配置备份和设备自身的恢复方案。SangforBox 尚未生成可认证的自动执行及回退计划。

## 二、获取建议并交接工程师

在 WorkBuddy 输入：“梳理 `<设备名>` 的应用控制策略，先给我审核表和问题表。”设备名是必填；可补充分析方向、受保护策略 ID 和命名前缀。不要在对话里提供设备密码。

WorkBuddy 调用 `analyze_app_control`，随后使用同一个 `job_id` 查询 `get_app_control_analysis`，直到 `state=completed`。得到 `analysis_id`、`proposal_hash`、`snapshot_hash` 后下载两份 CSV：

- 审核表：每个候选的编号、原配置、建议配置、证明状态、依赖与互斥项；默认“待确认”。
- 问题表：缺失字段、语义不支持、禁用策略、地址覆盖和其他分析限制。

工程师应核对目标设备、原策略 ID、顺序、对象完整成员、动作与附加选项。`unknown` 代表尚不能证明等价。`order` 类顺序建议可能改变访问行为，必须按业务意图单独判断。分析结果缺少动态语义时，不得把“未发现问题”解释为“全表安全”。

需求方在审核表中仅修改“用户决定”“用户备注”，或在 WorkBuddy 按变更编号明确说明采纳/不采纳。WorkBuddy 调用 `review_app_control` 后取得 `review_id`。随后用 `verify_app_control_review` 查看组合证明；需要再次核对设备状态时用只读 `preflight_app_control_review`。将 `review_id`、两份 CSV、组合验证结果和工单一起交给工程师。审核表内容或设备快照改变后，重新分析和审核，不沿用旧 `review_id`。

## 三、工程师手动实施

当前版本由工程师按照本单位变更流程在设备上手动操作。实施前确认设备当前配置仍与审核快照一致；若不一致，停止并重新分析。根据设备版本和业务要求，在维护窗口内逐项修改、逐项回读、持续观察连通性和策略命中。出现异常时按已批准的设备恢复方案处理，并在工单里记录实际步骤。

不得直接导入审核 CSV。不要把合并建议中的地址集合扩大成网段或域名通配符；不要只看允许/拒绝，还要核对日志、检测和顺序。工程师可以选择与建议不同的实现，但应在工单和反馈中说明原因。

## 四、提交负反馈并取回日志

工程师完成手动变更、回读设备后，在 WorkBuddy 输入：“按 `review_id=<编号>` 比较我手动修改后的当前应用控制配置，工单 `<工单号>`，工程师 `<标识>`，生成差异日志。”可再逐项补充候选编号、处理结论及原因，例如“`merge-...` 改造后采纳但用了不同对象”“`order-...` 因业务优先级不成立而拒绝”。

WorkBuddy 调用 `record_app_control_feedback`。最小请求为：

```json
{
  "request": {
    "review_id": "审核返回的 review_id",
    "submission_key": "本次反馈的稳定唯一编号",
    "engineer_label": "工程师标识",
    "change_ticket": "变更工单号"
  }
}
```

可选 `feedback_items` 中每项需要 `candidate_id`、`assessment`、`reason`。`assessment` 可填 `implemented`（按建议）、`modified`（变形实施）、`rejected`（拒绝）、`deferred`（延期）、`unsafe`（认为不安全）、`not_applicable`（不适用）。这些是工程师自述，系统不会把它们冒充设备实测事实。可选 `note` 记录整次变更背景。相同提交重试复用 `submission_key`，再次修改后重新比较时使用新的 key。

工具会重新完整读取应用控制策略、网络对象和策略组两遍，只有快照稳定时才比较。返回 `feedback_id`、对比状态及 `log_url`。WorkBuddy 应把 JSON 日志下载链接交回需求方；需求方日后可通过 `list_app_control_feedback(review_id)` 找回所有日志，通过 `get_app_control_feedback(feedback_id)` 找回指定日志。服务默认仅监听本机，远程访问需按部署要求配置受控入口。当前没有邮件或其他自动推送通道，工程师/WorkBuddy 须将链接或下载的文件发送给需求方。

日志包含分析、审核和建议的指纹，工程师工单与理由，原配置/建议配置/设备当前配置中有变化的记录，完整策略顺序和逐项差异。`matches_recommendation` 表示已读取到结构一致的配置；`unchanged_since_analysis` 表示当前结构与分析时相同；`differs_from_recommendation` 表示存在差异。状态只能说明本次快照观察到什么，不能证明是谁操作、业务意图正确或生产流量等价。`comparison_unavailable` 表示读取或比较失败，日志仍会保存失败记录，但没有可用的设备对比结论。

## 五、验收检查

在测试设备上分别验证：按建议实施、保持原配置、采用不同对象/动作、调整顺序、设备读取失败、重复提交。每次都应得到独立 `feedback_id` 和可下载的 JSON；同一个 `submission_key` 重试应返回同一日志。改变输入后复用 key 应被拒绝。工具调用全程不应出现设备写入。正式使用前需在目标 AF 版本上验证手动实施与回读语义；当前自动优化执行仍未完成。
