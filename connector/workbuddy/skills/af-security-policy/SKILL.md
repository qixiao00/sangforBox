---
name: af-security-policy
description: 通过 af-workbench MCP 查询、规划和变更深信服 AF 安全防护策略，包括业务防护、用户防护、外到内和内到外防护基线。
agent_created: true
---

# AF 安全防护策略

仅使用 `af-workbench` MCP 内部的 AF HTTP API。禁止读取设备凭据，禁止导入项目 Python 模块，禁止生成或运行直连设备脚本，禁止使用 curl。MCP 会话失败时停止并提示用户重新连接，不得改走脚本。

## MCP 能力

| 动作 | operation_id |
|---|---|
| 列表 | `policy.security.list` |
| 详情 | `policy.security.get` |
| 新增 | `policy.security.create` |
| 修改 | `policy.security.update` |
| 删除 | `policy.security.delete` |

安全防护策略已经是正式注册能力。不要再根据旧记录判断“只能直连 OpenAPI”。

## 标准流程

1. 用 `query_device` 查询 `policy.security.list`、`zone.list`、`network_object.list` 和 `interface.list`。
2. 区域方向、保护网段或模板不明确时询问用户，不按名称猜测。
3. 调用 `edit_device(action="prepare")`，把返回的必答问题逐项交给用户选择。
4. 将答案写入 change 的 `decisions`，调用 `edit_device(action="plan")`。请求体放在 `body`，保持扁平结构。
5. 向用户展示对象、方向、启用状态、模块、影响和回滚。用户确认后才调用 `edit_device(action="apply")`。
6. 用 `query_device` 回读验证策略内容、顺序和启用状态。

## 两种策略类型

| policyType | 场景 | 典型方向 |
|---|---|---|
| `SERVER` | 业务防护策略 | 外网区域到服务器区域 |
| `INTERNET_ACCESS` | 用户防护策略 | 内网客户端区域到外网区域 |

设备 8.0.107 实测约束：

- 请求体必须扁平，不得包裹 `security` 或 `obj`。
- `policyType` 使用 `SERVER` 或 `INTERNET_ACCESS`。
- `description` 不超过 95 字节，且不得包含设备禁止的特殊字符。
- 网络对象、区域和模板名称必须来自设备实时查询结果。
- 新增策略默认启用僵尸网络阻断：`response.block.enable=true`、`type=HIGH_THREATS`。用户明确要求关闭时保留用户选择。

这些约束已由 MCP 后端校验，禁止通过候选值循环试探设备。

## 输出

先给结论，再用一张小表展示名称、类型、源、目的、启用模块、顺序和状态。变更后说明验证结果、业务影响和回滚方式。省略请求体、接口地址、UUID、工具调用过程和调试信息，除非用户明确要求。
