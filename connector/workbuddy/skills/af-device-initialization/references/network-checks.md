# 网络设计字段与后端检查

使用初始化 Skill 整理拓扑和路由时读取。后端只校验结构化事实，不能理解一张拓扑图片或自然语言里的全部网络含义。

## 设计字段

`network_context` 是设计必填对象；旧案例须补齐后重新确认版本：

```json
{
  "network_context": {
    "needs_default_route": true,
    "routing_mode": "static",
    "uplink_interface": "eth1",
    "required_networks": ["172.16.10.0/24"]
  },
  "routing": [
    {"prefix": "0.0.0.0/0", "gateway": "198.51.100.1", "ifname": "eth1", "enable": true},
    {"prefix": "172.16.10.0/24", "gateway": "192.0.2.1", "ifname": "eth2", "enable": true}
  ]
}
```

示例地址仅解释字段，不能直接用于现场。每条下一跳、出接口和启用状态须来自工程师确认的设计。目标接口的地址、掩码和模式使用 AF 字段填入 `interfaces`，如：

```json
{"name":"eth1","ifMode":"ROUTE","shutdown":false,"ipv4":{"ipv4Mode":"STATIC","staticIp":[{"ipaddress":{"start":"198.51.100.2","end":"198.51.100.2","bits":24}}]}}
```

- `needs_default_route`：布尔值，表示业务是否需要默认路由。透明/虚拟网线部署不能自动填 true。
- `routing_mode`：`static`、`dynamic`、`none`。动态方式的运行路由尚未自动核实，保留人工验证项。
- `uplink_interface`：需要默认路由时填写真实业务上联接口；管理口不能作为业务出口。
- `required_networks`：必须核对去向的 IPv4 目标网段，包含拓扑中位于其他路由器后面的业务网段；直连网段可由接口地址验证。省略的目标不会被自动推断。默认路由不能证明到某个业务网段的具体去向正确。
- 三层设计的 `business_subnets` 也会加入检查，使用 IPv4 CIDR 字符串列表，或带 `prefix`/`cidr` 的对象列表。业务接口与受保护的管理网段重叠时会被拦截，避免改变直连 PC 的回程路径。
- `routing`：AF 路由对象列表。没有静态路由时传 `[]`，不能写“默认路由走出口”一类文本。

## 反馈与处理

`network_checks` 会在设计、盘点、准备、相关网络计划和初始化回读中返回；网络检查拦截时状态为 `network_check_blocked`。

| 字段 | 对话中的处理 |
| --- | --- |
| `management_connection`、`user_message` | 展示当前 WebAPI 管理 IP 和识别的接口，提醒保持不变 |
| `issues[].severity=blocker` | 配置冲突或触碰保护对象，停止该项变更 |
| `issues[].severity=required_context` | 询问缺失信息或说明不支持自动验证的原因；不能用一句“我确认”绕过硬检查 |
| `issues[].severity=warning` | 展示待实施、缺少路由、动态或多出口等事项，不能遗漏 |
| `existing_default_routes` | 展示已有默认路由，避免重复创建；新建同目的路由当前会要求重新明确方案，不能自动增设多出口 |
| `design_ready` | 网络设计是否具备确认条件；仍须同时满足 `missing_context` |
| `write_allowed` | 本次单项网络变更是否通过当前状态检查，不是下发授权 |
| `configuration_complete` | 当前受支持的配置检查是否还有问题；不代表业务连通 |
| `business_connectivity_verified`、`unverified` | 后端不自动证明实际业务、上游回程、动态/策略路由运行状态或远程管理客户端回程 |

默认路由缺失与“本次能否修改业务接口”是两回事。缺少的最终路由标为待处理，不能阻塞与其无关的早期步骤；当实际创建路由时，出接口必须已经存在、处于可验证的路由模式，下一跳必须能通过当前直连配置核对。

本版支持 IPv4 静态路由的配置检查。`auto` 仅在下一跳能唯一对应已启用三层接口时接受。递归路由、DHCP/PPPoE 自动路由、动态路由和策略路由不能根据静态清单判定已通。发现这些情况先说明证据不足，不得猜一个静态网关补上。

默认建议仍需工程师确认：按明确业务范围提出应用控制放通、NAT 和安全防护建议，标明范围、顺序、启用状态和依据。不得静默下发全局放通或未经确认的安全模板。
