SangforBox Windows 便携版
=========================

系统要求：64 位 Windows 10 或 Windows 11。

1. 将整个 SangforBox-Portable 文件夹复制到目标 Windows 电脑。不要只复制 EXE。
2. 双击 SangforBox.exe。首次启动会在 data 目录生成本地数据库并打开 http://127.0.0.1:8765/。
3. 在网页中添加 AF 设备档案并测试连接。设备密码使用当前 Windows 用户的 DPAPI 加密，只能由同一 Windows 用户解密。
4. 在 WorkBuddy 的“连接器”入口导入 WorkBuddy\sangforbox-workbuddy-connector.zip。
5. 如果使用 WorkBuddy 的“技能”入口，单独导入 WorkBuddy\af-app-control-optimization-skill.zip；该 ZIP 根目录直接包含 SKILL.md。
6. WorkBuddy MCP 地址固定为 http://127.0.0.1:8765/mcp/，无需请求头或令牌。

目录说明：
- data：设备档案、知识库索引、计划、案例和审核记录。不要在不同电脑之间复制已保存凭据。
- results：CSV、JSON 等导出结果。
- logs：启动和 HTTP 访问日志；单文件最多 5 MB，保留 3 个历史文件。
- WorkBuddy：连接器压缩包、应用控制梳理技能压缩包和工程师初始化指南。

停止服务：关闭 SangforBox 控制台窗口，或在窗口中按 Ctrl+C。
端口被占用时：先关闭已运行的 SangforBox；也可在命令行运行 SangforBox.exe --port 8766，但此时需同步修改 WorkBuddy 连接器地址。

安全提示：默认只监听本机 127.0.0.1。请勿把 MCP 端口直接暴露到局域网或互联网。
