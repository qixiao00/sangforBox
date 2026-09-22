SangforBox Windows 便携版
=========================

系统要求：64 位 Windows 10 或 Windows 11。

快速开始
--------
1. 将整个 SangforBox-Portable 文件夹复制到目标电脑。不要只复制 EXE。
2. 双击 SangforBox.exe。首次启动会在当前 Windows 用户的
   %LOCALAPPDATA%\SangforBox\ 生成数据库并打开 http://127.0.0.1:8765/。
3. 在网页中添加 AF 设备档案并测试连接。设备密码使用当前 Windows 用户的 DPAPI 加密，只能由同一 Windows 用户解密。
4. WorkBuddy 连接：在"连接器"入口导入 WorkBuddy\sangforbox-workbuddy-connector.zip；若用"技能"入口，把 WorkBuddy 下全部 *-skill.zip 逐个导入（每个 ZIP 根目录直接含 SKILL.md）。
   —— 连接器的 SKILL.md 是技能索引，不要当作技能导入。
5. MCP 地址固定为 http://127.0.0.1:8765/mcp/，无需请求头或令牌。

运行数据位置
------------
数据不在安装目录，而在当前 Windows 用户目录下：

  %LOCALAPPDATA%\SangforBox\
    af.db             设备档案、知识库索引、初始化案例
    policy-review.db  应用控制审核记录
    results\          审核 CSV、发现项 CSV、反馈 JSON
    logs\             启动和 HTTP 访问日志；单文件最多 5 MB，保留 3 个历史文件

因此可以直接用新版本覆盖替换安装目录，设备档案、审核记录和导出结果都不会丢失。
不要在不同电脑之间复制 data 下的数据库：已保存的设备凭据只能由原 Windows 用户解密。

安装目录说明
------------
- WorkBuddy：连接器压缩包、应用控制梳理技能压缩包和工程师初始化指南。
- 其余为程序文件，不含运行数据。

常见操作
--------
- 停止服务：关闭控制台窗口，或在窗口中按 Ctrl+C。
- 端口被占用：先关闭已运行的 SangforBox；也可运行 SangforBox.exe --port 8766，但需同步修改 WorkBuddy 连接器地址。
- 更换数据位置：设置环境变量 AF_STATE_DIR（整体）或 AF_DATABASE_PATH（仅数据库）。

安全提示
--------
默认只监听本机 127.0.0.1。请勿把 MCP 端口直接暴露到局域网或互联网。

更多说明
--------
完整功能、HTTP 接口、MCP 工具列表与开发说明见项目 README.md。
