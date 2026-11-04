SangforBox Windows 便携版
=========================

系统要求：64 位 Windows 10 或 Windows 11。

快速开始
--------
1. 将整个 SangforBox-Portable 文件夹复制到目标电脑。不要只复制 EXE。
2. 双击 SangforBox.exe。首次启动会在本目录生成 data、results、logs 并打开 http://127.0.0.1:8765/。
3. 在网页中添加 AF 设备档案并测试连接。设备密码使用当前 Windows 用户的 DPAPI 加密，只能由同一 Windows 用户解密。
4. WorkBuddy 连接：手工创建 MCP，名称填写 af-workbench，类型选择 Streamable HTTP，地址填写 http://127.0.0.1:8765/mcp/，不配置请求头或令牌。
5. 普通设备查询和目标明确的简单配置修改直接使用 MCP，无需导入 Skill。写入仍需经过 prepare、plan、明确确认 plan_id、apply 和回读验证。
6. 三个场景 Skill 按需导入：WorkBuddy\af-device-initialization-skill.zip（设备初始化）、WorkBuddy\af-app-control-optimization-skill.zip（应用控制策略梳理）、WorkBuddy\af-security-operations-skill.zip（安全告警处置与自运维）。只创建 MCP 不会自动启用 Skill；导入后新建对话并启用本次需要的 Skill。
7. 安全场景默认只读巡检并给出处置建议；只有工程师确认具体 plan_id 后，才可复用 MCP 完成简单配置处置和回读。它不自动封禁、不执行无人值守变更，完整威胁日志不可用时报告覆盖缺口。

运行数据位置
------------
全部运行数据都在 SangforBox.exe 所在的便携目录下：

  data\
    af.db             设备档案、知识库索引、初始化案例
    policy-review.db  应用控制审核记录
  results\            审核 CSV、发现项 CSV、反馈 JSON
  logs\               启动和 HTTP 访问日志；单文件最多 5 MB，保留 3 个历史文件

升级时保留 data、results、logs，只替换程序和 WorkBuddy 文件。复制整个目录可以带走数据，但已保存的设备凭据只能由原 Windows 用户解密；换用户或换电脑后需要重新录入密码。

安装目录说明
------------
- WorkBuddy：设备初始化、应用控制策略梳理、安全告警处置与自运维三个同级 Skill 压缩包；普通查询和简单配置不依赖 Skill。
- 其余为程序文件，不含运行数据。

常见操作
--------
- 停止服务：关闭控制台窗口，或在窗口中按 Ctrl+C。
- 端口被占用：先关闭已运行的 SangforBox；也可运行 SangforBox.exe --port 8766，但需同步修改 WorkBuddy MCP 地址。
- 更换数据位置：设置环境变量 AF_STATE_DIR（整体）、AF_DATABASE_PATH（仅主数据库）或 AF_OUTPUT_DIR（仅导出目录）。审核数据库始终与主数据库放在同一目录。

安全提示
--------
默认只监听本机 127.0.0.1。请勿把 MCP 端口直接暴露到局域网或互联网。

更多说明
--------
完整功能、HTTP 接口、MCP 工具列表与开发说明见项目 README.md。
