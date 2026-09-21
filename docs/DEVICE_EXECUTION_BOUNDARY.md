# 设备执行边界

## 目标

WorkBuddy 负责理解用户需求和调用 Skill；SangforBox MCP 是唯一允许认证并访问 AF 设备的组件。查询、修改、初始化和后续新增场景都遵守同一边界。

## 开发模式与生产模式

当前开发模式下，WorkBuddy 与 SangforBox 运行在同一 Windows 用户中。Skill 可以规定“不写直连脚本”，但同一用户仍可能读取项目模块、解密 DPAPI 凭据并直接访问设备，因此提示词不是强安全边界。

生产离线版应采用以下部署：

```text
WorkBuddy + AF Skills
        │ 仅 MCP/HTTP localhost
        ▼
SangforBox Windows Service（独立服务账户）
        │ 持有设备凭据、审计、计划与执行权限
        ▼
AF 设备管理网络
```

## 必须满足的隔离条件

1. SangforBox 后端作为 Windows Service 运行，使用独立的低权限服务账户。
2. 设备凭据由该服务账户加密，凭据文件 ACL 只允许服务账户读取。
3. WorkBuddy 用户不能读取服务账户的凭据存储，也不能导入服务内部的设备客户端模块。
4. Windows Defender Firewall 只允许 SangforBox 服务进程访问 AF 管理地址；阻止 WorkBuddy、其 Python 和 shell 进程访问设备管理网段。
5. Web 页面和 WorkBuddy 都只调用本机 SangforBox API/MCP，不接收明文设备密码。
6. 每次设备调用记录来源、设备、endpoint_id/operation_id、计划、确认、结果和回读验证。

## 通用能力模型

- 已审核的高频场景使用稳定 `operation_id`。
- 其他手册接口使用知识库中的 `endpoint_id` 动态解析。
- MCP 从索引读取固定 method、path 和参数约束；客户端不能传任意 URL。
- GET 进入查询流程；POST、PUT、PATCH、DELETE 进入 `prepare → plan → apply → verify`。
- Skill 只描述厂商业务语义、必答问题和验证标准，不包含 HTTP 客户端、凭据或脚本。

完成上述隔离后，即使 Agent 生成设备直连脚本，脚本也无法读取凭据或连接 AF；所有有效设备操作只能经过 MCP。
