---
name: af-virtual-wire
description: 通过 af-workbench MCP 查询、配置或回滚深信服 AF 虚拟网线组及其接口和区域归属。
agent_created: true
---

# AF 虚拟网线

仅使用 `af-workbench` MCP，默认走 AF HTTP API。不得读取本地凭据、生成 Python/curl 脚本或绕过 MCP。会话失效时提示重新连接。

## 查询

一次调用 `query_device(action="execute")`，operation_ids 使用：

- `virline.list`：虚拟网线组
- `zone.list`：区域及接口成员
- 需要接口状态时增加 `interface.list`

只输出目标网线组、相关接口和 `forwardType=VIRTUAL` 的相关区域，不展开其他区域。接口启用状态以 `shutdown` 字段为准；区域方向无法由配置证明时标注待确认。

## 变更

通过 `edit_device` 执行 `discover/describe → prepare → 用户回答必答问题 → plan → 用户确认 → apply → 回读验证`。虚拟网线是多对象联动，计划必须按以下顺序展开：

1. 从不兼容的原区域逐个移除接口。
2. 将两个物理口的 `ifMode` 改为 `VIRTUALLINE`。
3. 创建或修改虚拟网线组。
4. 将两端接口分别加入 `forwardType=VIRTUAL` 的区域。

模式切换可能清空原 VLAN 配置，计划中必须明确列出。不要自动启用接口，不要根据区域名称推断内外网方向。

## 输出

查询结果先给一句结论，再用一张表列：网线组、端口对、区域归属、接口状态。变更结果只列实际变化、验证结果、业务影响和回滚方式。省略 API 路径、UUID、工具过程、Session/PID 和原始 JSON，除非用户明确要求。
