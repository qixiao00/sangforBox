---
name: af-blacklist
description: 通过 af-workbench MCP 查询、新增、修改或删除深信服 AF 黑白名单 IP 与域名条目。
agent_created: true
layer: scenario
version: 1.0.0
---

# AF 黑白名单

仅使用 `af-workbench` MCP 的 AF HTTP API。查询使用 `blacklist.list/get`，变更使用 `blacklist.create/update/delete`，遵循 `discover/describe → prepare → 用户回答必答问题 → plan → 用户确认 → apply → 回读验证`。

请求体使用扁平 `body`。`type` 为 `BLACK` 或 `WHITE`，`urlType` 为 `ip` 或 `domain`。新增前查询目标是否已存在；不得修改系统默认白名单。黑名单和安全策略拒绝规则是不同机制，用户意图不明确时先说明作用范围。

输出只包含：目标、名单类型、启用状态、执行/验证结果。发生冲突或引用系统条目时说明原因。省略完整名单、请求体、API 路径和内部调用过程。回滚为删除新建条目，修改操作则恢复变更前快照。
