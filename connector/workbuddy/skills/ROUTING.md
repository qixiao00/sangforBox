# AF Skill 路由表

本文件是所有 AF 场景路由规则的**唯一事实来源**。`af-framework` 与各场景 Skill
只引用本表，不再各自重写路由决策树。新增 Skill 时只改这一处。

## 路由规则

| 触发意图 | 路由到 | 边界 |
|---|---|---|
| 梳理、优化、批量规范命名、找重复/遮蔽/未生效策略、合并、对象化、分析顺序或例外关系 | `af-app-control-optimization` | 只到只读预检；不执行设备写入 |
| 明确指定单条应用控制策略的新增、修改、删除并给出完整目标条件 | `af-app-control-policy` | `prepare → plan → apply → verify` |
| 虚拟网线组、接口与区域归属的查询、配置或回滚 | `af-virtual-wire` | `prepare → plan → apply → verify` |
| 黑白名单 IP 与域名条目的查询、新增、修改、删除 | `af-blacklist` | `prepare → plan → apply → verify` |
| 安全防护策略（业务防护、用户防护、外到内、内到外）的查询与变更 | `af-security-policy` | `prepare → plan → apply → verify` |
| 引导工程师完成设备初始化设计、分阶段实施与回读 | `af-device-initialization` | `initialize_device` 的分阶段流程 |
| 工程师现场调整后的差异确认、逐项满意度评价与案例日志 | `af-initialization-feedback` | 只读回读 + 案例记录 |
| 通用查询（当前配置、当前状态、是否存在） | `af-framework` | `query_device`，以设备实时数据为准 |
| 通用变更（手册接口但无业务快捷名称） | `af-framework` | `edit_device` 的 `prepare → plan → apply → verify` |
| 字段含义、版本支持、怎么配置 | `af-framework` | 走知识库工具，不当作设备现状 |

## 冲突与边界判定

- **"梳理后直接执行"** → 仍走 `af-app-control-optimization`，只到只读预检。用户说"全部采纳"
  不等于设备执行授权，不能转成通用 `edit_device` 或 `initialize_device` 变更。
- **"调整顺序使其生效" / "按梳理建议执行"** → 属于整表效果判断，进入
  `af-app-control-optimization`；只有用户明确指定某条策略的目标位置且意图是直接配置时，
  才进入 `af-app-control-policy`。
- **同时提出"新增一条指定策略"和"梳理整表"** → 拆成两个任务分别说明：整表梳理先产出审核表，
  单条配置在用户给出明确条件后才进入 `af-app-control-policy` 的独立计划流程。
- **优化审核项及 `review_id`** → 不能作为 `af-app-control-policy` 的 `edit_device` 请求体
  或执行授权，不能当作单条配置请求。
- **设备 ID 查询、连接测试** → 走 `query_device`，不得用 `initialize_device`。
- **"当前配置是否存在"用设备实时数据；"字段含义/版本支持"用知识库** → 不要把手册定义
  当成设备现状。

## 不分层的约束

以下约束适用于全部场景 Skill，由 `af-framework` 统一承载，场景 Skill 不重复声明：

- 所有 AF 设备操作必须通过 `af-workbench` MCP。
- 禁止生成 Python、PowerShell、Bash、curl 或其他设备直连方式；禁止读取凭据库；
  禁止导入项目设备客户端模块。
- MCP 不可用时停止设备操作并报告阻塞原因，不得改走旁路。
- 注册表没有业务快捷名称时，使用知识库返回的 `endpoint_id`，不能判定为"不可执行"。
- 未注册接口使用 `endpoint_id`，已注册接口使用 `operation_id`，二者只填一个。

## 维护

1. 新增场景 Skill：在本表加一行，并在 `SKILL.md` 的 `description` 里写入对应触发条件。
2. 修改触发词：只改本表与目标 Skill 的 `description`，不要在其它 Skill 的正文里同步措辞。
3. 字段规范见 [`_SCHEMA.md`](_SCHEMA.md)。
