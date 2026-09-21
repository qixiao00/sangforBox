# WorkBuddy Skill 集合

SangforBox 连接器同时分发 MCP 配置和 AF 场景 Skill。用户导入连接器后，WorkBuddy 使用 Skill 理解厂商场景和编排步骤，所有设备读写仍由 MCP 完成。

## 分层

| 层 | 职责 | 不负责 |
|---|---|---|
| `af-framework` | 全局路由、通用 endpoint_id 执行规则、安全边界、输出规范 | 具体业务字段取值 |
| 场景 Skill | 某个 AF 功能的前置查询、提问、字段语义、验证和回滚 | 登录、凭据、HTTP 请求和脚本执行 |
| MCP | 设备选择、认证、参数校验、计划、确认、执行、审计和回读 | 自然语言业务判断 |

## 当前场景

- `af-virtual-wire`：虚拟网线及接口、区域联动。
- `af-blacklist`：黑白名单。
- `af-app-control-policy`：用户明确指定的单条应用控制策略配置。
- `af-app-control-optimization`：整表梳理、优化候选、审核 CSV、组合复验、只读预检及工程师手动变更后的差异日志。
- `af-security-policy`：业务防护和用户防护策略。
- `af-device-initialization`：多轮收集设计、分阶段初始化和回读。
- `af-initialization-feedback`：现场调整后的差异确认、逐项 CSV 评价和案例日志。

工程师操作说明随连接器分发：[`docs/af-initialization-engineer-guide.md`](docs/af-initialization-engineer-guide.md)。

## 测试能力沉淀流程

1. 先通过 MCP 完成测试，保留接口、请求字段、设备返回和版本信息。
2. 如果设备行为与手册不一致，把确定性的限制加入 MCP 校验层。
3. 把场景判断、前置查询、用户必答问题、验证标准和输出格式写入独立 Skill。
4. Skill 中只引用 `operation_id` 或 `endpoint_id`，不得包含设备地址、账号、密码、DPAPI 解密或 AF 客户端代码。
5. 使用 `package-workbuddy.ps1` 打包，导入新连接器验证 Tool 与 Skill 同时可见。

## 强制规则

- 所有 AF 设备操作通过 `af-workbench` MCP。
- Skill 不得生成 Python、PowerShell、Bash、curl 或其他设备直连方式。
- 注册表没有业务快捷名称时，使用知识库返回的 `endpoint_id`，不能判定为“不可执行”。
- 单条配置修改流程固定为 `prepare → 用户回答 → plan → 用户确认 → apply → 回读验证`；整表优化当前只到审核、复验和只读预检，不得转入通用修改流程。
- MCP 不可用时停止设备操作并报告原因。

新场景请从 [`skill-template/SKILL.template.md`](skill-template/SKILL.template.md) 复制。
