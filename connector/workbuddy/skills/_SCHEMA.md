# Skill frontmatter 规范

本目录下每个 `<skill-name>/SKILL.md` 的 YAML frontmatter 必须包含以下字段，缺一不可。
新增 Skill 时从 `skill-template/SKILL.template.md` 复制，不要手写字段。

## 必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 与所在目录名完全一致，`af-` 前缀，仅小写字母、数字和连字符 |
| `description` | string | 一句话。必须同时说明**触发条件**和**边界**（能做什么、不能做什么） |
| `agent_created` | bool | 固定 `true`。标识该 Skill 由模型创建，允许后续用 SkillManage 维护 |
| `layer` | enum | `framework` 或 `scenario`，见下节 |
| `version` | string | 语义化版本，如 `1.0.0`。内容有实质修改时递增 |

## 分层取值

| layer | Skill | 含义 |
|---|---|---|
| `framework` | `af-framework` | 全局路由、通用执行规则、安全边界、输出规范 |
| `scenario` | 其余全部 | 单个 AF 功能的前置查询、必答问题、字段语义、验证与回滚 |

全局只允许存在一个 `framework`，其余一律为 `scenario`。

## 模板

```yaml
---
name: af-feature-name
description: <触发条件>；<边界，含何时不适用>。
agent_created: true
layer: scenario
version: 1.0.0
---
```

## 约束

- `description` 中的触发词与 `ROUTING.md` 保持一致，不要在 `description` 里重复整棵路由决策树。
- 不要在 frontmatter 里写设备地址、账号、密码或任何凭据。
- 字段顺序固定为 `name`、`description`、`agent_created`、`layer`、`version`，便于逐行比对。
