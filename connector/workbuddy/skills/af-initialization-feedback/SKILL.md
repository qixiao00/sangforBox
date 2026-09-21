---
name: af-initialization-feedback
description: 工程师调整 AF 初始化配置后，通过 SangforBox MCP 回读现场状态，确认差异，并把逐项满意度 CSV 保存到初始化案例。
---

# AF 初始化反馈

只使用 `af-workbench` MCP；所有设备读取使用 `initialize_device` 或 `query_device`。不得读取设备凭据、导入设备客户端、拼接设备 URL 或生成直连脚本。MCP 无法访问设备时报告阻塞，不使用旁路。

1. 向工程师取得初始化 `case_id`；不清楚时从当前对话已保存的案例结果中查找，不猜测。调用 `initialize_device(action="case", case_id=...)` 核对设备和设计版本。请工程师描述现场改过什么及原因，作为解释线索。
2. 调用 `initialize_device(action="feedback_review", case_id=...)`。它回读同一设备，返回逐项的实施前配置、工具实施后快照、当前配置和差异，并标出工程师新增或删除的配置。回读失败或截断时停止评价，先恢复可靠查询。
3. 用小表向工程师展示全部差异，确认对象身份和差异是否属实。无法关联的对象和无法捕获的实施快照如实标注。配置被修改不自动算不满意，没被修改也不自动算满意。
4. 交付 MCP 返回的 `csv_template` 供工程师填写。保持 `case_id`、`item_id`、`review_hash` 和配置列不变；每行填写 `difference_confirmed`（`yes`、`no`、`uncertain`）及 `satisfaction`（`satisfied`、`unsatisfied`）。不满意行须填 `reason`；`comment` 可补现场原因和建议。每一行都要评价，包括未调整项和非计划改动。
5. 将工程师填好的 CSV 原文传给 `initialize_device(action="feedback_submit", case_id=..., feedback_csv=...)`。MCP 核对案例、配置项和回读指纹后保存。若提示快照变化，重新 `feedback_review` 并请工程师复核，不能直接覆盖旧评价。
6. 用 `initialize_device(action="events", case_id=...)` 获取阶段、结果和时间的审计记录。仅在工程师排障时展示。事件不包含设备密码、Cookie、Token 或完整请求体。

输出简短差异表、满意/不满意数量、未解决问题和建议调整的默认规则。现场反馈属于该案例；更新通用 Skill 或 MCP 字段校验前，应核对设备版本、手册证据与可复现结果。
