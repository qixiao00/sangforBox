---
name: af-app-control-policy
description: 通过 af-workbench MCP 查询、规划和变更用户明确指定的单条深信服 AF 应用控制策略，例如按区域允许或拒绝指定应用。
agent_created: true
layer: scenario
version: 1.0.0
---

# AF 应用控制策略

本 Skill 只处理用户明确指定的单条策略配置。整表梳理、批量命名、遮蔽检测、合并、地址对象化
及顺序调整属于其它 Skill，见 [`ROUTING.md`](ROUTING.md)。

**边界**：优化审核项及 `review_id` 不能作为本 Skill 的 `edit_device` 请求体或执行授权。

## 标准流程

1. 使用 `query_device` 查询 `zone.list`、`policy.app_control.list`、`policy.app_control.group.list`、`network_object.list`。
2. 使用 `application.library.list` 确认应用的完整 `appPath`。
3. 使用 `edit_device describe` 获取策略请求定义。
4. 使用 `edit_device prepare` 获取新增/修改、同名对象处理和策略启用状态等必答问题，并逐项询问用户。
5. 将用户答案写入 `decisions`，使用 `edit_device plan` 生成变更，展示后等待用户确认，再 `apply`。
6. 回读策略，验证启用状态、动作、源/目的区域、地址对象、应用路径和策略顺序。

设备 8.0.107 实测：请求体使用扁平 `body`；`labels` 使用数组；内建全部地址对象可能名为 `All`；应用引用使用完整 `appPath`。这些值仍应从目标设备回读确认。

拒绝策略不得把应用回退为“全部”。无法找到精确应用时停止生成计划。策略创建成功不代表生效，必须检查它是否位于会命中的顺序位置。

## 输出

先说明策略是否存在、是否启用、是否具备生效条件。表格最多列策略名、动作、源区域、目的区域、应用、顺序和状态。变更后补充验证结果、业务影响和回滚方式。省略应用库全量数据、UUID、请求体、重试过程和内部报错试探，除非用户要求排障细节。
