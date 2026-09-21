---
name: af-feature-name
description: <触发条件>；<边界，含何时不适用>。
agent_created: true
layer: scenario
version: 1.0.0
---

> frontmatter 字段规范见 [`skills/_SCHEMA.md`](../skills/_SCHEMA.md)。路由触发词见 [`skills/ROUTING.md`](../skills/ROUTING.md)，不要在本文件重复整棵路由决策树。

# AF 功能名称

仅使用 `af-workbench` MCP。禁止读取凭据、导入项目客户端、拼接设备 URL 或生成直连脚本。

## 业务目标

说明此功能解决的 AF 运维问题、关键对象和适用版本。

## MCP 能力

| 动作 | operation_id 或 endpoint_id 来源 |
|---|---|
| 查询 | `feature.list`，或通过 `lookup_api_endpoint` 选择 GET endpoint_id |
| 新增 | `feature.create`，或选择 POST endpoint_id |
| 修改 | `feature.update`，或选择 PUT/PATCH endpoint_id |
| 删除 | `feature.delete`，或选择 DELETE endpoint_id |

## 前置查询

列出生成计划前必须从设备读取的配置。不要根据对象名称猜测用途。

## 必答问题

先调用 `edit_device(action="prepare")`，逐项询问 MCP 返回的问题。补充本场景独有且无法从设备确定的问题。

## 计划与执行

1. 将用户答案写入 change 的 `decisions`。
2. 调用 `edit_device(action="plan")`。
3. 展示对象、变化、启用状态、影响和回滚。
4. 用户确认后调用 `edit_device(action="apply")`。
5. 使用 `query_device` 回读验证。

## 产品约束

只记录经过手册或设备实测确认的约束。确定性字段限制应同时实现到 MCP 后端校验层。

## 输出

先给结论，只展示相关对象、验证结果、业务影响和回滚方式。省略内部调用过程和原始 JSON。
