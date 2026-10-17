"""Deterministic checks for AF configuration; no device I/O or topology inference."""

from __future__ import annotations

import ipaddress
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class NetworkContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    needs_default_route: StrictBool
    routing_mode: Literal["static", "dynamic", "none"]
    uplink_interface: str = ""
    required_networks: list[str] = Field(default_factory=list)


class NetworkSafetyError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("; ".join(x["message"] for x in report["issues"]
                                  if x["severity"] in {"blocker", "required_context"}))


def issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def rows(results: list[dict], operation: str) -> list[dict]:
    for result in results:
        if result.get("operation_id") != operation or not result.get("ok"):
            continue
        data = result.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("items", "rows", "list"):
                if isinstance(data.get(key), list):
                    return [x for x in data[key] if isinstance(x, dict)]
    return []


def inventory_complete(data: Any, limit: int) -> bool:
    """A full page without a proven end, unknown shape or bounded data is incomplete."""
    def arrays_fit(value):
        if isinstance(value, list):
            return len(value) < limit and all(arrays_fit(x) for x in value)
        return not isinstance(value, dict) or all(arrays_fit(x) for x in value.values())

    if not arrays_fit(data):
        return False
    if isinstance(data, list):
        return len(data) < limit and all(isinstance(x, dict) for x in data)
    if not isinstance(data, dict):
        return False
    for key in ("items", "rows", "list"):
        if key in data:
            values = data[key]
            if not isinstance(values, list) or len(values) >= limit:
                return False
            try:
                if "totalItems" in data and int(data["totalItems"]) > len(values):
                    return False
                if int(data.get("itemsOffset", 0)) != 0:
                    return False
                if int(data.get("totalPages", 1)) > 1 or int(data.get("pageNumber", 1)) > 1:
                    return False
            except (ValueError, TypeError):
                return False
            return all(isinstance(x, dict) for x in values)
    return False


def interface_addresses(row: dict) -> list[ipaddress.IPv4Interface | ipaddress.IPv6Interface]:
    addresses = []
    for family in ("ipv4", "ipv6"):
        config = row.get(family) or {}
        if not isinstance(config, dict):
            continue
        for entry in config.get("staticIp") or []:
            if not isinstance(entry, dict):
                continue
            value = entry.get("ipaddress", entry)
            if not isinstance(value, dict):
                continue
            try:
                # AF's documented address fields; descriptions/gateways are not interface IPs.
                addresses.append(ipaddress.ip_interface(f"{value['start']}/{value['bits']}"))
            except (ValueError, KeyError, TypeError):
                continue
    return addresses


def interface_ips(row: dict) -> set:
    """Identify an owning IP even when mask information is missing (routing stays unknown)."""
    addresses = set()
    for family in ("ipv4", "ipv6"):
        config = row.get(family)
        if not isinstance(config, dict):
            continue
        for entry in config.get("staticIp") or []:
            if isinstance(entry, dict):
                value = entry.get("ipaddress", entry)
                try:
                    addresses.add(ipaddress.ip_address(value["start"]))
                except (ValueError, KeyError, TypeError):
                    pass
    return addresses


def identities(row: dict) -> set[str]:
    return {str(row[k]) for k in ("name", "uuid") if row.get(k)}


def references(value: Any, candidates: set[str]) -> bool:
    if isinstance(value, dict):
        return any(key in candidates or references(x, candidates) for key, x in value.items())
    if isinstance(value, list):
        return any(references(x, candidates) for x in value)
    return isinstance(value, str) and value in candidates


def merge(previous: dict, patch: dict) -> dict:
    result = dict(previous)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def resource_family(path: str) -> str:
    resource = path.split("/namespaces/{namespace}/", 1)[-1]
    if resource == "staticroutes/ipv4" or resource.startswith("staticroutes/ipv4/"):
        return "routes"
    for root in ("interfaces", "zones", "virlines", "localappcontrols"):
        if resource == root or resource.startswith(root + "/"):
            return root
    return ""


def management_connection(base_url: str, results: list[dict]) -> dict:
    host = urlparse(base_url).hostname or ""
    report: dict = {"address": host, "interface": None, "protected_interfaces": [],
                    "protected_zones": [], "protected_virlines": [], "identified": False,
                    "message": "", "issues": []}
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        report["issues"].append(issue("management_unknown", "required_context",
            f"当前 Web API 地址 {host!r} 不是直接 IP，无法可靠识别管理接口；请核实直连地址。"))
        return report
    interfaces = rows(results, "interface.list")
    owners = [x for x in interfaces if address in interface_ips(x)]
    if len(owners) != 1:
        report["issues"].append(issue("management_unknown", "required_context",
            f"无法唯一识别当前 Web API 地址 {host} 所属管理接口，暂停网络变更。"))
        return report
    owner = owners[0]
    protected = identities(owner)
    if not protected:
        report["issues"].append(issue("management_unknown", "required_context", "管理接口缺少名称和 UUID，无法锁定保护对象，暂停网络变更。"))
        return report
    if address not in {a.ip for a in interface_addresses(owner)}:
        report["issues"].append(issue("management_mask_unknown", "required_context", "管理地址已识别，但缺少可靠的掩码，无法校验业务网段冲突，暂停网络变更。"))
    # Close over documented parent/aggregate relationships, including the other members.
    while True:
        before = set(protected)
        for row in interfaces:
            relation = {k: row[k] for k in ("subif", "channelif") if k in row}
            if identities(row) & protected or references(relation, protected):
                protected.update(identities(row))
                for candidate in interfaces:
                    if references(relation, identities(candidate)):
                        protected.update(identities(candidate))
        for line in rows(results, "virline.list"):
            if references(line, protected):
                for candidate in interfaces:
                    if references({"line1": line.get("line1"), "line2": line.get("line2")}, identities(candidate)):
                        protected.update(identities(candidate))
        if protected == before:
            break
    label = owner.get("name") or owner.get("uuid")
    report.update(identified=True, interface=label, protected_interfaces=sorted(protected))
    for operation, key in (("zone.list", "protected_zones"), ("virline.list", "protected_virlines")):
        report[key] = sorted({v for row in rows(results, operation) if references(row, protected)
                              for v in identities(row)})
    report["message"] = (f"当前通过 {host} 连接防火墙，管理接口为 {label}。"
                         "初始化期间保留该管理 IP、掩码和接口配置，业务网络使用其他接口；"
                         "PC 直连管理口时，不能把 PC 地址当作业务默认网关。")
    return report


def check_route(route: dict, interfaces: list[dict], protected: set[str]) -> list[dict]:
    problems = []
    try:
        if not isinstance(route.get("prefix"), str) or not isinstance(route.get("gateway"), str):
            raise ValueError("Explicit prefix and gateway are required")
        prefix = ipaddress.IPv4Network(route["prefix"], strict=False)
        gateway = ipaddress.IPv4Address(route["gateway"])
    except (ValueError, KeyError, TypeError):
        return [issue("route_address_invalid", "blocker", "路由目的网段或下一跳缺失/格式无效，需使用明确的 IPv4 网段和地址。")]
    all_ips = {address for row in interfaces for address in interface_ips(row)}
    if gateway in all_ips:
        problems.append(issue("gateway_is_self", "blocker", f"路由 {prefix} 的下一跳 {gateway} 是防火墙自身地址。"))
    if gateway.is_multicast or gateway == ipaddress.IPv4Address("255.255.255.255"):
        problems.append(issue("gateway_invalid", "blocker", f"路由 {prefix} 的下一跳不是有效单播地址。"))
    if str(route.get("tableId", "")) == "250":
        problems.append(issue("management_route", "blocker", "初始化不能修改管理网络路由表（tableId=250）。"))
    ifname = route.get("ifname")
    if not ifname:
        return problems + [issue("route_interface_missing", "required_context", f"路由 {prefix} 缺少出接口，请确认 ifname。")]
    if ifname == "auto":
        candidates = [row for row in interfaces if any(gateway in a.network for a in interface_addresses(row))]
        candidates = [row for row in candidates if row.get("shutdown") is not True and row.get("ifMode") == "ROUTE"]
        if not gateway.is_unspecified and len(candidates) == 1:
            selected = candidates[0]
        else:
            return problems + [issue("route_interface_unresolved", "required_context",
                f"路由 {prefix} 的 auto 出接口不能唯一解析；请指定可验证的接口，递归/动态路由需单独核实。")]
    else:
        matches = [row for row in interfaces if ifname in identities(row)]
        if len(matches) != 1:
            return problems + [issue("route_interface_missing", "blocker", f"路由出接口 {ifname} 不存在或不能唯一识别。")]
        selected = matches[0]
    if identities(selected) & protected:
        problems.append(issue("management_business_route", "blocker", "业务路由不能使用当前 Web API 管理接口作为出接口。"))
    if selected.get("shutdown") is True:
        problems.append(issue("route_interface_disabled", "blocker", f"路由出接口 {ifname} 已禁用。"))
    if selected.get("ifMode") != "ROUTE":
        problems.append(issue("route_interface_mode", "required_context", f"路由出接口 {ifname} 尚未确认为 ROUTE 模式。"))
    addresses = interface_addresses(selected)
    if not gateway.is_unspecified and not any(gateway in a.network for a in addresses):
        problems.append(issue("gateway_not_on_link", "required_context",
            f"下一跳 {gateway} 不在出接口 {ifname} 的已知直连网段，当前不支持证明递归路由可达；请核实。"))
    for address in addresses:
        if address.version == 4 and address.network.prefixlen < 31 and gateway in {address.network.network_address, address.network.broadcast_address}:
            problems.append(issue("gateway_network_address", "blocker", f"下一跳 {gateway} 是出接口网段的网络或广播地址。"))
    return problems


def network_report(base_url: str, results: list[dict], *, step: dict | None = None,
                   path: str = "", method: str = "", design: dict | None = None) -> dict:
    management = management_connection(base_url, results)
    problems = list(management["issues"])
    interfaces = rows(results, "interface.list")
    protected = set(management["protected_interfaces"])
    family = resource_family(path)
    body = (step or {}).get("body") or {}
    target_ids = {str(x) for x in ((step or {}).get("path_parameters") or {}).values()}
    if step and family:
        # Unknown management identity prevents all writes that can affect the connection.
        if family == "localappcontrols":
            problems.append(issue("management_acl", "blocker",
                "当前无法证明本机访问控制变更会保留 Web API 会话，禁止自动修改；请通过带外条件手工处理。"))
        dependencies = protected if family == "interfaces" else set(management.get("protected_" + family, []))
        body_ids = identities(body)
        if (target_ids | body_ids) & dependencies or references(body, protected):
            problems.append(issue("management_dependency", "blocker",
                f"禁止修改当前 Web API 地址 {management['address']} 所属管理接口及其关联配置。"))
        if family in {"interfaces", "zones", "virlines"} and method != "POST" and not target_ids:
            problems.append(issue("unidentified_network_write", "blocker", "禁止无法识别目标或批量替换的网络配置变更。"))
        if family in {"interfaces", "zones", "virlines"} and method != "POST":
            operation = {"interfaces": "interface.list", "zones": "zone.list", "virlines": "virline.list"}[family]
            if not any(identities(row) & target_ids for row in rows(results, operation)):
                problems.append(issue("network_target_unknown", "blocker", "目标网络配置不在完整盘点中，无法校验关联影响。"))
        if family == "interfaces":
            try:
                management_ip = ipaddress.ip_address(management["address"])
            except ValueError:
                management_ip = None
            if management_ip in interface_ips(body):
                problems.append(issue("management_ip_duplicate", "blocker", "其他接口不能配置当前 Web API 管理 IP。"))
            if any(k in body for k in ("ipv4", "ipv6", "ifMode", "shutdown")) and body.get("shutdown") is not True:
                previous = next((row for row in interfaces if identities(row) & target_ids), {})
                proposed = merge(previous, body) if method == "PATCH" else body
                management_networks = [a.network for row in interfaces if identities(row) & protected
                                       for a in interface_addresses(row)]
                if any(a.network.version == network.version and a.network.overlaps(network)
                       for a in interface_addresses(proposed) for network in management_networks):
                    problems.append(issue("management_subnet_overlap", "blocker", "业务接口网段与受保护的管理网段重叠，可能改变直连 PC 的回程路径；请改用其他业务网段。"))
        if family == "routes":
            current = [r for r in rows(results, "route.ipv4.list") if identities(r) & target_ids]
            if method in {"PATCH", "PUT", "DELETE"} and len(current) != 1:
                problems.append(issue("route_target_unknown", "blocker", "无法从完整路由清单唯一定位变更目标。"))
            route = merge(current[0], body) if current and method == "PATCH" else (current[0] if method == "DELETE" and current else body)
            if current and (str(current[0].get("tableId", "")) == "250" or current[0].get("ifname") in protected):
                problems.append(issue("management_route", "blocker", "禁止修改或删除原有管理网络路由。"))
            if method != "DELETE":
                problems.extend(check_route(route, interfaces, protected))
            if method == "POST":
                for existing in rows(results, "route.ipv4.list"):
                    try:
                        same_prefix = ipaddress.IPv4Network(existing["prefix"], strict=False) == ipaddress.IPv4Network(route["prefix"], strict=False)
                    except (ValueError, KeyError, TypeError):
                        same_prefix = False
                    if same_prefix:
                        problems.append(issue("route_prefix_exists", "required_context",
                            f"已存在目的网段 {route.get('prefix')} 的路由；请核实已有路由及多出口意图，不能重复新增。"))
                        break

    # Design checking is against the final intended state; missing live routes do not block
    # the earlier interface/object stages. Proposed individual writes still use live state.
    design = design or {}
    context = design.get("network_context")
    final_interfaces = {str(x.get("name")): x for x in interfaces}
    for proposed in design.get("interfaces", []) if isinstance(design.get("interfaces"), list) else []:
        if isinstance(proposed, dict) and proposed.get("name"):
            previous = final_interfaces.get(str(proposed["name"]), {})
            if identities(previous) & protected and any(k in proposed and proposed[k] != previous.get(k)
                                                        for k in ("ipv4", "ipv6", "ifMode", "shutdown", "name", "description", "manage", "mtu", "subif", "channelif", "oobManage")):
                problems.append(issue("design_management_change", "blocker", "目标设计试图改变当前管理口，请改用业务接口。"))
            elif any(k in proposed and proposed[k] != previous.get(k)
                     for k in ("ipv4", "ipv6", "ifMode", "shutdown", "name")):
                problems.append(issue("interface_pending", "warning", f"接口 {proposed['name']} 的目标网络配置尚未全部实施并回读。"))
            final_interfaces[str(proposed["name"])] = merge(previous, proposed)
    final_interfaces_list = list(final_interfaces.values())
    actual_routes = [r for r in rows(results, "route.ipv4.list") if r.get("enable") is True
                     and str(r.get("tableId", "")) in {"", "0", "251"}]
    if any(not isinstance(r.get("enable"), bool) for r in rows(results, "route.ipv4.list")):
        problems.append(issue("route_state_unverified", "warning", "部分静态路由缺少明确启用状态，不能当作有效路径。"))
    proposed_routes = design.get("routing") if isinstance(design.get("routing"), list) else []
    for route in proposed_routes:
        if isinstance(route, dict):
            for problem in check_route(route, final_interfaces_list, protected):
                problems.append({**problem, "scope": "design"})
    if not isinstance(context, dict):
        problems.append(issue("network_context_missing", "required_context",
            "请确认 network_context：是否需要默认路由、路由方式、业务上联接口及必须可达的目标网段。"))
    else:
        try:
            context = NetworkContext.model_validate(context).model_dump()
            required = [str(ipaddress.IPv4Network(x, strict=False)) for x in context["required_networks"]]
            if context["routing_mode"] != "none":
                business = design.get("business_subnets", [])
                for subnet in business if isinstance(business, list) else [business]:
                    candidate = (subnet.get("prefix") or subnet.get("cidr")) if isinstance(subnet, dict) else subnet
                    try:
                        if not isinstance(candidate, str):
                            raise ValueError("Expected a CIDR string")
                        required.append(str(ipaddress.IPv4Network(candidate, strict=False)))
                    except (ValueError, TypeError):
                        problems.append(issue("business_subnet_unknown", "required_context", "业务网段必须填写明确的 IPv4 CIDR（或 prefix/cidr 字段），才能核对路由去向。"))
            if context["needs_default_route"]:
                required.append("0.0.0.0/0")
            if context["routing_mode"] == "dynamic":
                problems.append(issue("dynamic_routes_unverified", "warning", "动态/策略路由未采集运行表，不能证明目标网络可达，也不能据此自动新增静态默认路由。"))
            if context["needs_default_route"] and not context["uplink_interface"]:
                problems.append(issue("uplink_missing", "required_context", "需要默认路由时，请明确业务上联接口。"))
            if context["uplink_interface"] in protected:
                problems.append(issue("management_uplink", "blocker", "业务上联不能选择当前管理接口。"))
            if context["uplink_interface"] and not any(context["uplink_interface"] in identities(row) for row in final_interfaces_list):
                problems.append(issue("uplink_unknown", "required_context", "业务上联接口未出现在设备现状或目标接口设计中，请核实接口名称。"))
            if context["routing_mode"] == "none" and (context["needs_default_route"] or context["required_networks"]):
                problems.append(issue("routing_mode_conflict", "required_context", "routing_mode=none 与需要三层路由的目标冲突，请确认部署方式。"))
            planned = [r for r in proposed_routes if isinstance(r, dict) and r.get("enable") is not False]
            if any(not isinstance(r, dict) for r in proposed_routes) or not isinstance(design.get("routing"), list):
                problems.append(issue("routing_unstructured", "required_context", "routing 必须使用结构化路由列表，不能从自然语言猜测下一跳。"))
            if context["routing_mode"] != "dynamic":
                for prefix in dict.fromkeys(required):
                    network = ipaddress.IPv4Network(prefix)
                    candidates = []
                    for route in actual_routes + planned:
                        try:
                            route_network = ipaddress.IPv4Network(route["prefix"], strict=False)
                            matches_uplink = True
                            if network.prefixlen == 0 and context["uplink_interface"]:
                                matches_uplink = route.get("ifname") == context["uplink_interface"]
                                if route.get("ifname") == "auto":
                                    matches_uplink = any(row.get("name") == context["uplink_interface"]
                                        and any(ipaddress.IPv4Address(route["gateway"]) in a.network for a in interface_addresses(row))
                                        for row in final_interfaces_list)
                            if (network.subnet_of(route_network) and (network.prefixlen == 0 or route_network.prefixlen > 0)
                                    and matches_uplink and not check_route(route, final_interfaces_list, protected)):
                                candidates.append(route)
                        except (ValueError, KeyError, TypeError):
                            pass
                    connected = any(network.subnet_of(a.network) for row in final_interfaces_list
                                    if row.get("ifMode") == "ROUTE" and row.get("shutdown") is not True
                                    for a in interface_addresses(row) if a.version == 4)
                    if not candidates and not connected:
                        problems.append(issue("route_missing", "warning", f"目标设计缺少到 {prefix} 的已验证配置路径；请提出路由建议并确认下一跳，当前不能宣告初始化完成。"))
                    elif candidates and not any(r in actual_routes for r in candidates):
                        problems.append(issue("route_pending", "warning", f"到 {prefix} 的路由已列入设计，尚需实施并回读。"))
        except (ValueError, TypeError) as exc:
            problems.append(issue("network_context_invalid", "required_context", f"network_context 无效：{exc}"))
    defaults = [r for r in actual_routes if r.get("prefix") == "0.0.0.0/0"]
    if any(isinstance(row.get("ipv4"), dict) and row["ipv4"].get("ipv4Mode") in {"DHCP", "PPPOE"} for row in interfaces):
        problems.append(issue("automatic_route_unverified", "warning", "接口使用 DHCP/PPPoE，静态路由清单不能证明自动默认路由是否存在；新增前需核实运行状态。"))
    if len(defaults) > 1:
        problems.append(issue("multiple_default_routes", "warning", "存在多条启用的静态默认路由，请核实优先级、链路探测和多出口意图。"))
    return {"management_connection": management, "issues": problems,
            "write_allowed": not any(x["severity"] in {"blocker", "required_context"} and x.get("scope") != "design"
                                     and x["code"] not in {"network_context_missing", "uplink_missing", "network_context_invalid", "routing_unstructured"}
                                     for x in problems),
            "design_ready": not any(x["severity"] in {"blocker", "required_context"} for x in problems),
            "configuration_complete": not problems, "business_connectivity_verified": False,
            "existing_default_routes": defaults,
            "unverified": ["实际业务连通性、上游回程路由、动态/策略路由运行状态、远程管理客户端的回程路径需现场验证。"],
            "user_message": management["message"]}
