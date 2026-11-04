"use client";

import {
  Activity, BookOpenText, Boxes, Braces, Check, ChevronRight, CircleDot, Clock3,
  Command, Database, FileText, HardDrive, KeyRound, Layers3, Link2, Menu, Network,
  Pencil, Play, Plus, RefreshCw, RotateCcw, Search, ServerCog, Settings, ShieldCheck,
  TerminalSquare, Trash2, Wifi, X, Zap,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

type View = "console" | "knowledge" | "devices";
type EventStatus = "running" | "completed" | "failed" | "cancelled";
type ProbeResult = { message: string; database: string; schema_version: number };
type TraceEvent = { request_id: string; actor: string; actor_type: string; parent_id: string | null; status: EventStatus; message: string; timestamp: string; elapsed_ms: number; result?: ProbeResult };
type DeviceProfile = { device_id: string; name: string; base_url: string; namespace: string; username: string; verify_tls: boolean; ca_bundle: string | null; timeout_seconds: number; enabled: boolean; has_credentials: boolean; created_at: string; updated_at: string };
type DeviceAudit = { audit_id: number; device_id: string; success: boolean; stage: string; message: string; latency_ms: number; tested_at: string };
type ConnectionResult = { ok: boolean; device_id: string; device_name: string; base_url: string; namespace: string; username: string; role: string | null; auth_result: string | null; reported_namespace: string | null; keepalive_ok: boolean; latency_ms: number; tested_at: string };
type DeviceForm = { name: string; base_url: string; namespace: string; username: string; password: string; verify_tls: boolean; ca_bundle: string; timeout_seconds: number; enabled: boolean };
type KnowledgeDocument = { doc_id: string; title: string; doc_type: string; vendor: string | null; product: string | null; version: string | null; source_path: string; sha256: string; page_count: number; processed_at: string | null; chunks: number; api_endpoints: number; cli_commands: number };
type KnowledgeOverview = { stats: { documents: number; chunks: number; api_endpoints: number; cli_commands: number; indexed: boolean; last_imported_at: string | null; manifest_sha256: string | null; retrieval: string }; documents: KnowledgeDocument[]; imports: { import_id: number; source_manifest_sha256: string; imported_at: string; documents: number; chunks: number; api_endpoints: number; cli_commands: number }[]; index_health: { engine: string; tokenizer: string; chunk_rows: number; fts_rows: number; healthy: boolean; coverage_percent: number } };
type SearchHit = { chunk_id: string; doc_id: string; doc_type: string; title: string | null; page_start: number; page_end: number; score: number; snippet: string };

const emptyDeviceForm: DeviceForm = { name: "", base_url: "https://", namespace: "public", username: "", password: "", verify_tls: true, ca_bundle: "", timeout_seconds: 15, enabled: true };
const stages = [
  { actor: "Web", label: "请求入口", icon: Network },
  { actor: "MCP", label: "工具协议", icon: Boxes },
  { actor: "SQLite", label: "本地数据", icon: Database },
];
const nav = [
  { id: "console" as View, label: "运行台", icon: Activity },
  { id: "knowledge" as View, label: "知识库", icon: BookOpenText },
  { id: "devices" as View, label: "设备", icon: ServerCog },
  { id: null, label: "连接器", icon: Link2 },
];

function formatClock(value?: string) {
  if (!value) return "--:--:--";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value));
}
function formatDate(value?: string) {
  if (!value) return "暂无记录";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}
async function responseError(response: Response) {
  try {
    const payload = await response.json();
    const detail = payload.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message as string;
  } catch { /* status fallback */ }
  return `请求失败（HTTP ${response.status}）`;
}

export default function Home() {
  const [activeView, setActiveView] = useState<View>("console");
  const [message, setMessage] = useState("检查 AF 工作台框架链路");
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const [devices, setDevices] = useState<DeviceProfile[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(false);
  const [deviceError, setDeviceError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [deviceForm, setDeviceForm] = useState<DeviceForm>(emptyDeviceForm);
  const [savingDevice, setSavingDevice] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [connectionResult, setConnectionResult] = useState<ConnectionResult | null>(null);
  const [audits, setAudits] = useState<DeviceAudit[]>([]);
  const [knowledge, setKnowledge] = useState<KnowledgeOverview | null>(null);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [knowledgeError, setKnowledgeError] = useState("");
  const [searchQuery, setSearchQuery] = useState("编辑接口");
  const [searchHits, setSearchHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);

  const latest = events.at(-1);
  const result = latest?.result;
  const complete = latest?.status === "completed";
  const requestId = events[0]?.request_id;
  const selectedDevice = devices.find((device) => device.device_id === selectedId) ?? null;
  const actorStates = useMemo(() => Object.fromEntries(stages.map((stage) => [stage.actor, events.filter((item) => item.actor === stage.actor).at(-1)])), [events]);

  useEffect(() => {
    const syncView = () => setActiveView(window.location.hash === "#devices" ? "devices" : window.location.hash === "#knowledge" ? "knowledge" : "console");
    syncView();
    window.addEventListener("hashchange", syncView);
    return () => window.removeEventListener("hashchange", syncView);
  }, []);
  useEffect(() => { if (activeView === "devices") void loadDevices(); }, [activeView]);
  useEffect(() => { if (activeView === "knowledge") void loadKnowledge(); }, [activeView]);

  async function loadKnowledge() {
    setKnowledgeLoading(true); setKnowledgeError("");
    try {
      const response = await fetch("/api/knowledge/overview", { cache: "no-store" });
      if (!response.ok) throw new Error(await responseError(response));
      setKnowledge(await response.json() as KnowledgeOverview);
    } catch (reason) { setKnowledgeError((reason as Error).message || "无法读取知识库状态"); }
    finally { setKnowledgeLoading(false); }
  }

  async function searchKnowledge(event: FormEvent) {
    event.preventDefault(); if (!searchQuery.trim()) return;
    setSearching(true); setKnowledgeError("");
    try {
      const response = await fetch("/api/knowledge/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query: searchQuery.trim(), top_k: 5 }) });
      if (!response.ok) throw new Error(await responseError(response));
      setSearchHits((await response.json()).hits as SearchHit[]);
    } catch (reason) { setKnowledgeError((reason as Error).message || "检索抽查失败"); }
    finally { setSearching(false); }
  }

  async function loadDevices(preferredId?: string) {
    setDevicesLoading(true); setDeviceError("");
    try {
      const response = await fetch("/api/devices", { cache: "no-store" });
      if (!response.ok) throw new Error(await responseError(response));
      const items = await response.json() as DeviceProfile[];
      setDevices(items);
      const candidate = preferredId ?? selectedId ?? items[0]?.device_id ?? null;
      const nextId = items.some((item) => item.device_id === candidate) ? candidate : items[0]?.device_id ?? null;
      setSelectedId(nextId);
      if (nextId) void loadAudits(nextId); else setAudits([]);
    } catch (reason) { setDeviceError((reason as Error).message || "无法读取设备配置"); }
    finally { setDevicesLoading(false); }
  }
  async function loadAudits(deviceId: string) {
    try {
      const response = await fetch(`/api/devices/${deviceId}/audits?limit=20`, { cache: "no-store" });
      if (!response.ok) throw new Error(await responseError(response));
      setAudits(await response.json() as DeviceAudit[]);
    } catch (reason) { setDeviceError((reason as Error).message || "无法读取连接记录"); }
  }
  function selectDevice(deviceId: string) {
    setSelectedId(deviceId); setConnectionResult(null); setDeviceError(""); void loadAudits(deviceId);
  }
  function beginCreate() { setEditingId("new"); setDeviceForm({ ...emptyDeviceForm }); setDeviceError(""); }
  function beginEdit(device: DeviceProfile) {
    setSelectedId(device.device_id); setEditingId(device.device_id); setDeviceError("");
    setDeviceForm({ name: device.name, base_url: device.base_url, namespace: device.namespace, username: device.username, password: "", verify_tls: device.verify_tls, ca_bundle: device.ca_bundle ?? "", timeout_seconds: device.timeout_seconds, enabled: device.enabled });
  }
  async function saveDevice(event: FormEvent) {
    event.preventDefault();
    if (!editingId) return;
    if (editingId === "new" && !deviceForm.password) { setDeviceError("新设备必须填写 API 账户密码"); return; }
    setSavingDevice(true); setDeviceError("");
    try {
      const payload: Record<string, unknown> = { name: deviceForm.name.trim(), base_url: deviceForm.base_url.trim(), namespace: deviceForm.namespace.trim(), username: deviceForm.username.trim(), verify_tls: deviceForm.verify_tls, ca_bundle: deviceForm.ca_bundle.trim() || null, timeout_seconds: Number(deviceForm.timeout_seconds), enabled: deviceForm.enabled };
      if (deviceForm.password) payload.password = deviceForm.password;
      const isNew = editingId === "new";
      const response = await fetch(isNew ? "/api/devices" : `/api/devices/${editingId}`, { method: isNew ? "POST" : "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (!response.ok) throw new Error(await responseError(response));
      const saved = await response.json() as DeviceProfile;
      setEditingId(null); setDeviceForm({ ...emptyDeviceForm }); await loadDevices(saved.device_id); setSelectedId(saved.device_id);
    } catch (reason) { setDeviceError((reason as Error).message || "设备配置保存失败"); }
    finally { setSavingDevice(false); }
  }
  async function testDevice(device: DeviceProfile) {
    setTestingId(device.device_id); setSelectedId(device.device_id); setConnectionResult(null); setDeviceError("");
    try {
      const response = await fetch(`/api/devices/${device.device_id}/test`, { method: "POST" });
      if (!response.ok) throw new Error(await responseError(response));
      setConnectionResult(await response.json() as ConnectionResult);
    } catch (reason) { setDeviceError((reason as Error).message || "连接测试失败"); }
    finally { setTestingId(null); await loadAudits(device.device_id); }
  }
  async function deleteDevice(device: DeviceProfile) {
    if (!window.confirm(`删除设备“${device.name}”及其本地连接记录？`)) return;
    setDeviceError("");
    try {
      const response = await fetch(`/api/devices/${device.device_id}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response));
      setEditingId(null); setConnectionResult(null); await loadDevices();
    } catch (reason) { setDeviceError((reason as Error).message || "设备删除失败"); }
  }
  async function runProbe(event: FormEvent) {
    event.preventDefault(); if (!message.trim()) return; abortRef.current?.abort();
    const controller = new AbortController(); abortRef.current = controller; setRunning(true); setEvents([]); setError("");
    try {
      const response = await fetch("/api/test?stream=true", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: message.trim() }), signal: controller.signal });
      if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`);
      if (!response.body) throw new Error("浏览器未收到事件流");
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
      while (true) {
        const { value, done } = await reader.read(); buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
        const lines = buffer.split("\n"); buffer = lines.pop() ?? "";
        for (const line of lines) if (line.trim()) setEvents((current) => [...current, JSON.parse(line)]);
        if (done) break;
      }
      if (buffer.trim()) setEvents((current) => [...current, JSON.parse(buffer)]);
    } catch (reason) { if ((reason as Error).name !== "AbortError") setError((reason as Error).message || "请求未完成"); }
    finally { setRunning(false); }
  }
  function reset() { abortRef.current?.abort(); setEvents([]); setError(""); setRunning(false); }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${menuOpen ? "sidebar--open" : ""}`}>
        <div className="brand"><div className="brand__mark"><ShieldCheck size={23} strokeWidth={2.2} /></div><div><strong>SangforBox</strong><span>AF CONTROL PLANE</span></div></div>
        <button className="sidebar__close" onClick={() => setMenuOpen(false)} aria-label="关闭菜单"><X /></button>
        <nav aria-label="主导航"><p className="nav-label">工作区</p>
          {nav.map(({ id, label, icon: Icon }) => {
            const active = id === activeView;
            return <button key={label} className={active ? "nav-item nav-item--active" : "nav-item"} disabled={!id} title={id ? label : `${label}将在后续版本接入`} onClick={() => { if (id) { window.location.hash = id === "console" ? "" : id; setActiveView(id); setMenuOpen(false); } }}><Icon size={19} /><span>{label}</span>{!id && <small>待接入</small>}</button>;
          })}
        </nav>
        <div className="sidebar__foot"><div className="health-dot" /><div><span>本地服务</span><strong>运行中 · 8765</strong></div><Settings size={18} /></div>
      </aside>
      {menuOpen && <button className="scrim" onClick={() => setMenuOpen(false)} aria-label="关闭菜单遮罩" />}
      <main>
        <header className="topbar"><button className="menu-button" onClick={() => setMenuOpen(true)} aria-label="打开菜单"><Menu /></button><div><span className="eyebrow">AF 智能运维底座</span><h1>{activeView === "console" ? "运行台" : activeView === "devices" ? "设备连接" : "知识库"}</h1></div><div className="topbar__status"><CircleDot size={15} /><span>MCP ONLINE</span><code>/mcp/</code></div></header>

        {activeView === "console" ? <div className="workspace">
          <section className="control-zone">
            <div className="intro"><div><span className="section-number">01</span><span className="eyebrow">链路验证</span></div><h2>看见每一次请求<br />经过哪里。</h2><p>Web 请求统一进入 MCP，由本地服务完成工具调用和数据访问。右侧会显示当前正在工作的模块。</p></div>
            <form className="probe-form" onSubmit={runProbe}><label htmlFor="message"><span><TerminalSquare size={16} />测试消息</span><em>{message.length} / 500</em></label><textarea id="message" value={message} onChange={(e) => setMessage(e.target.value.slice(0, 500))} rows={4} /><div className="form-actions"><button className="primary-button" disabled={running || !message.trim()}>{running ? <><span className="spinner" />正在执行</> : <><Play size={17} fill="currentColor" />运行链路测试</>}</button><button className="icon-button" type="button" onClick={reset} aria-label="重置运行记录" title="重置"><RotateCcw size={18} /></button></div>{error && <div className="error-banner" role="alert"><X size={17} />{error}</div>}</form>
            <div className="architecture-note"><Zap size={17} /><div><strong>统一执行面</strong><p>页面与 WorkBuddy 最终调用相同的 MCP 工具，权限、日志和设备操作规则只维护一份。</p></div></div>
          </section>
          <section className="trace-zone" aria-live="polite">
            <div className="trace-head"><div><span className="section-number section-number--light">02</span><span className="eyebrow">实时工作日志</span></div><span className={`run-state ${running ? "run-state--live" : complete ? "run-state--done" : ""}`}><i />{running ? "执行中" : complete ? "已完成" : "等待请求"}</span></div>
            <div className="signal-path">{stages.map(({ actor, label, icon: Icon }, index) => { const item = actorStates[actor] as TraceEvent | undefined; const state = item?.status ?? "idle"; return <div className="stage-wrap" key={actor}><div className={`stage stage--${state}`}><div className="stage__icon"><Icon size={19} />{state === "completed" && <span><Check size={11} /></span>}</div><div><strong>{actor}</strong><small>{label}</small></div><em>{state === "idle" ? "待命" : item?.elapsed_ms === 0 ? "运行" : `${item?.elapsed_ms} ms`}</em></div>{index < stages.length - 1 && <div className={`connector ${item ? "connector--active" : ""}`}><i /></div>}</div>; })}</div>
            <div className="log-panel"><div className="log-panel__meta"><span>EVENT STREAM</span><code>{requestId ? `#${requestId.slice(0, 8)}` : "NO REQUEST"}</code></div><div className="log-list">{events.length === 0 ? <div className="empty-state"><Activity size={28} /><strong>尚无执行记录</strong><span>运行测试后，这里会逐条显示真实事件。</span></div> : events.map((item, index) => <div className={`log-entry log-entry--${item.status}`} key={`${item.actor}-${index}`}><span className="log-entry__rail"><i /></span><time>{formatClock(item.timestamp)}</time><div><strong>{item.actor}</strong><p>{item.message}</p></div><span className="log-entry__duration">{item.elapsed_ms} ms</span></div>)}</div></div>
            {result && <div className="result-strip"><div><Check size={16} /><span>数据库连接</span><strong>{result.database}</strong></div><div><span>Schema</span><strong>v{result.schema_version}</strong></div><button type="button">查看详情 <ChevronRight size={15} /></button></div>}
          </section>
        </div> : activeView === "devices" ? <section className="device-workspace">
          <div className="device-hero"><div><span className="eyebrow">API CONNECTION VAULT</span><h2>把设备接进来，<br />先验证每一步。</h2><p>账户密码由 Windows DPAPI 加密。测试只执行登录、Token 保活与注销，不修改设备配置。</p></div><div className="device-hero__actions"><button className="secondary-button" onClick={() => void loadDevices()} disabled={devicesLoading}><RefreshCw size={16} />刷新</button><button className="primary-button" onClick={beginCreate}><Plus size={17} />添加设备</button></div></div>
          {deviceError && <div className="device-error" role="alert"><X size={17} /><span>{deviceError}</span><button onClick={() => setDeviceError("")} aria-label="关闭"><X size={14} /></button></div>}
          <div className="device-grid">
            <div className="device-inventory"><div className="panel-heading"><div><span>设备资产</span><strong>{devices.length.toString().padStart(2, "0")}</strong></div><small>本地加密存储</small></div>
              {devicesLoading && devices.length === 0 ? <div className="device-empty"><span className="spinner spinner--dark" /><strong>正在读取设备</strong></div> : devices.length === 0 ? <div className="device-empty"><ServerCog size={34} /><strong>还没有设备</strong><p>添加一个已开启 WEB API 权限的 AF 管理账户。</p><button onClick={beginCreate}><Plus size={15} />添加第一台设备</button></div> : <div className="device-list">{devices.map((device, index) => <article key={device.device_id} className={`device-card ${selectedId === device.device_id ? "device-card--selected" : ""}`} onClick={() => selectDevice(device.device_id)}><div className="device-card__index">{String(index + 1).padStart(2, "0")}</div><div className="device-card__body"><div className="device-card__title"><strong>{device.name}</strong><span className={device.enabled ? "status-chip status-chip--ready" : "status-chip"}>{device.enabled ? "已启用" : "已停用"}</span></div><code>{device.base_url}</code><div className="device-card__meta"><span><KeyRound size={13} />{device.username}</span><span>NS / {device.namespace}</span></div></div><ChevronRight size={17} /></article>)}</div>}
            </div>
            <div className="device-detail">
              {editingId ? <form className="device-form" onSubmit={saveDevice}>
                <div className="detail-head"><div><span className="eyebrow">{editingId === "new" ? "NEW CONNECTION" : "EDIT CONNECTION"}</span><h3>{editingId === "new" ? "添加 AF 设备" : "编辑连接配置"}</h3></div><button type="button" className="icon-button icon-button--small" onClick={() => setEditingId(null)} aria-label="关闭编辑"><X size={17} /></button></div>
                <div className="form-grid"><label><span>设备名称</span><input required value={deviceForm.name} onChange={(e) => setDeviceForm({ ...deviceForm, name: e.target.value })} placeholder="例如：总部出口 AF" /></label><label><span>Namespace</span><input required value={deviceForm.namespace} onChange={(e) => setDeviceForm({ ...deviceForm, namespace: e.target.value })} /></label><label className="form-span"><span>设备地址</span><input required type="url" value={deviceForm.base_url} onChange={(e) => setDeviceForm({ ...deviceForm, base_url: e.target.value })} placeholder="https://192.168.1.1" /></label><label><span>WEB API 账户</span><input required value={deviceForm.username} onChange={(e) => setDeviceForm({ ...deviceForm, username: e.target.value })} autoComplete="username" /></label><label><span>{editingId === "new" ? "账户密码" : "账户密码（留空保留）"}</span><input required={editingId === "new"} type="password" value={deviceForm.password} onChange={(e) => setDeviceForm({ ...deviceForm, password: e.target.value })} autoComplete="new-password" /></label><label><span>连接超时（秒）</span><input type="number" min="1" max="120" value={deviceForm.timeout_seconds} onChange={(e) => setDeviceForm({ ...deviceForm, timeout_seconds: Number(e.target.value) })} /></label><label><span>CA 文件路径（可选）</span><input value={deviceForm.ca_bundle} onChange={(e) => setDeviceForm({ ...deviceForm, ca_bundle: e.target.value })} placeholder="C:\certs\af-ca.pem" /></label></div>
                <div className="switch-row"><label><input type="checkbox" checked={deviceForm.verify_tls} onChange={(e) => setDeviceForm({ ...deviceForm, verify_tls: e.target.checked })} /><span><strong>校验 TLS 证书</strong><small>生产环境建议开启</small></span></label><label><input type="checkbox" checked={deviceForm.enabled} onChange={(e) => setDeviceForm({ ...deviceForm, enabled: e.target.checked })} /><span><strong>启用设备</strong><small>停用后禁止连接测试</small></span></label></div>
                <div className="form-actions form-actions--end"><button type="button" className="secondary-button" onClick={() => setEditingId(null)}>取消</button><button className="primary-button" disabled={savingDevice}>{savingDevice ? <><span className="spinner" />正在保存</> : "保存设备"}</button></div>
              </form> : selectedDevice ? <>
                <div className="detail-head"><div><span className="eyebrow">SELECTED DEVICE</span><h3>{selectedDevice.name}</h3><code>{selectedDevice.base_url}</code></div><div className="detail-actions"><button className="icon-button icon-button--small" onClick={() => beginEdit(selectedDevice)} title="编辑"><Pencil size={16} /></button><button className="icon-button icon-button--small icon-button--danger" onClick={() => void deleteDevice(selectedDevice)} title="删除"><Trash2 size={16} /></button></div></div>
                <div className="connection-strip"><div><span>账户</span><strong>{selectedDevice.username}</strong></div><i /><div><span>命名空间</span><strong>{selectedDevice.namespace}</strong></div><i /><div><span>TLS</span><strong>{selectedDevice.verify_tls ? "校验证书" : "不校验"}</strong></div></div>
                <button className="connection-test" disabled={testingId === selectedDevice.device_id || !selectedDevice.enabled} onClick={() => void testDevice(selectedDevice)}><span className="connection-test__icon">{testingId ? <span className="spinner" /> : <Wifi size={22} />}</span><span><strong>{testingId ? "正在连接设备" : "测试 API 连接"}</strong><small>登录 → Cookie Token → Keepalive → 注销</small></span><ChevronRight size={18} /></button>
                {connectionResult && connectionResult.device_id === selectedDevice.device_id && <div className="connection-success"><Check size={17} /><div><strong>连接验证通过</strong><span>{connectionResult.role ?? "未知角色"} · {connectionResult.auth_result ?? "未知认证"} · {connectionResult.latency_ms} ms</span></div></div>}
                <div className="audit-block"><div className="audit-head"><div><Clock3 size={15} /><strong>连接记录</strong></div><button onClick={() => void loadAudits(selectedDevice.device_id)}>刷新</button></div>{audits.length === 0 ? <div className="audit-empty">尚未测试这台设备</div> : audits.map((audit) => <div className="audit-row" key={audit.audit_id}><span className={audit.success ? "audit-dot audit-dot--ok" : "audit-dot"} /><div><strong>{audit.success ? "连接成功" : `连接失败 · ${audit.stage}`}</strong><small>{audit.message}</small></div><div><strong>{audit.latency_ms} ms</strong><time>{formatDate(audit.tested_at)}</time></div></div>)}</div>
              </> : <div className="detail-placeholder"><ShieldCheck size={36} /><strong>本地凭据保险箱</strong><p>选择一台设备查看连接状态，或添加新的 AF 设备。</p></div>}
            </div>
          </div>
        </section> : <section className="knowledge-workspace">
          <div className="knowledge-hero">
            <div><span className="eyebrow">LOCAL KNOWLEDGE INDEX</span><h2>每一份输入，<br />都有可追踪的去向。</h2><p>在这里查看原始数据、切分结果、结构化抽取和全文索引状态，并用真实查询抽查检索质量。</p></div>
            <button className="secondary-button" onClick={() => void loadKnowledge()} disabled={knowledgeLoading}><RefreshCw size={16} />刷新索引状态</button>
          </div>
          {knowledgeError && <div className="device-error" role="alert"><X size={17} /><span>{knowledgeError}</span><button onClick={() => setKnowledgeError("")} aria-label="关闭"><X size={14} /></button></div>}
          <div className="knowledge-metrics">
            <div><FileText size={17} /><span>数据源</span><strong>{knowledge?.stats.documents.toLocaleString() ?? "—"}</strong></div>
            <div><Layers3 size={17} /><span>切分块</span><strong>{knowledge?.stats.chunks.toLocaleString() ?? "—"}</strong></div>
            <div><Braces size={17} /><span>API 定义</span><strong>{knowledge?.stats.api_endpoints.toLocaleString() ?? "—"}</strong></div>
            <div><Command size={17} /><span>CLI 命令</span><strong>{knowledge?.stats.cli_commands.toLocaleString() ?? "—"}</strong></div>
            <div className={knowledge?.index_health.healthy ? "metric-health metric-health--ok" : "metric-health"}><CircleDot size={17} /><span>索引覆盖</span><strong>{knowledge ? `${knowledge.index_health.coverage_percent}%` : "—"}</strong></div>
          </div>
          <div className="knowledge-grid">
            <div className="source-panel">
              <div className="knowledge-panel-head"><div><span className="eyebrow">INPUT SOURCES</span><h3>输入数据</h3></div><small>{knowledgeLoading ? "正在读取" : `${knowledge?.documents.length ?? 0} 个已导入文档`}</small></div>
              <div className="source-list">
                {knowledgeLoading && !knowledge ? <div className="knowledge-loading"><span className="spinner spinner--dark" />读取知识库元数据</div> : knowledge?.documents.map((document) => (
                  <article className="source-card" key={document.doc_id}>
                    <div className="source-card__icon">{document.doc_type === "api_manual" ? <Braces size={20} /> : document.doc_type === "cli_manual" ? <Command size={20} /> : <BookOpenText size={20} />}</div>
                    <div className="source-card__body"><div><strong>{document.title}</strong><span>{document.doc_type === "api_manual" ? "API 手册" : document.doc_type === "cli_manual" ? "CLI 手册" : "产品手册"}</span></div><code title={document.source_path}>{document.source_path.split(/[\\/]/).at(-1)}</code><div className="source-card__facts"><span>{document.page_count.toLocaleString()} 页</span><span>{document.chunks.toLocaleString()} chunks</span>{document.api_endpoints > 0 && <span>{document.api_endpoints.toLocaleString()} API</span>}{document.cli_commands > 0 && <span>{document.cli_commands.toLocaleString()} CLI</span>}</div></div>
                    <div className="source-card__state"><Check size={13} /><span>已索引</span><small>v{document.version ?? "—"}</small></div>
                  </article>
                ))}
              </div>
              <div className="source-ingest-note"><HardDrive size={18} /><div><strong>当前数据入口</strong><p>由本地 `knowledge/processed` 目录导入。后续上传 PDF、删除数据源和重建索引会接入同一位置。</p></div></div>
            </div>
            <div className="index-column">
              <div className="index-panel">
                <div className="knowledge-panel-head"><div><span className="eyebrow">INDEX PIPELINE</span><h3>索引编制</h3></div><span className={knowledge?.index_health.healthy ? "index-badge index-badge--ok" : "index-badge"}>{knowledge?.index_health.healthy ? "健康" : "待检查"}</span></div>
                <div className="pipeline">
                  {[{ label: "文档解析", value: `${knowledge?.stats.documents ?? 0} 份` }, { label: "语义切分", value: `${knowledge?.index_health.chunk_rows.toLocaleString() ?? 0} 块` }, { label: "结构抽取", value: `${((knowledge?.stats.api_endpoints ?? 0) + (knowledge?.stats.cli_commands ?? 0)).toLocaleString()} 条` }, { label: "FTS5 入库", value: `${knowledge?.index_health.fts_rows.toLocaleString() ?? 0} 条` }].map((step, index) => <div className="pipeline-step" key={step.label}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{step.label}</strong><small>{step.value}</small></div><Check size={14} /></div>)}
                </div>
                <div className="index-meta"><div><span>检索引擎</span><strong>{knowledge?.index_health.engine ?? "—"}</strong></div><div><span>分词</span><strong>{knowledge?.index_health.tokenizer ?? "—"}</strong></div><div><span>最近导入</span><strong>{formatDate(knowledge?.stats.last_imported_at ?? undefined)}</strong></div></div>
              </div>
              <div className="search-panel">
                <div className="knowledge-panel-head"><div><span className="eyebrow">RETRIEVAL CHECK</span><h3>检索抽查</h3></div><small>返回真实 chunk</small></div>
                <form className="knowledge-search" onSubmit={searchKnowledge}><Search size={16} /><input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="输入手册问题或 API 名称" /><button disabled={searching}>{searching ? "检索中" : "检索"}</button></form>
                <div className="search-results">{searchHits.length === 0 ? <div className="search-empty"><Search size={23} /><span>输入问题，检查索引能否命中正确页码。</span></div> : searchHits.map((hit) => <article key={hit.chunk_id}><div><strong>{hit.title ?? hit.doc_id}</strong><span>PDF {hit.page_start === hit.page_end ? `P${hit.page_start}` : `P${hit.page_start}–${hit.page_end}`}</span></div><p>{hit.snippet}</p><code>{hit.chunk_id}</code></article>)}</div>
              </div>
            </div>
          </div>
        </section>}
      </main>
    </div>
  );
}
