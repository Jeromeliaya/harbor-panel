"use strict";

const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const icon = (name, extra = "") => `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const views = {
  overview: { title: "主机概览", kicker: "WORKSPACE OVERVIEW", description: "一切尽在掌握，开始今天的运维。", icon: "grid" },
  files: { title: "文件管理", kicker: "FILE WORKSPACE", description: "浏览、创建和编辑文件，让每一份配置各就其位。", icon: "folder" },
  containers: { title: "Docker 容器", kicker: "CONTAINER MANAGEMENT", description: "连接本机 Docker，管理容器生命周期与日志。", icon: "box" },
  terminal: { title: "诊断终端", kicker: "HOST DIAGNOSTICS", description: "用熟悉的命令，快速了解主机的真实状态。", icon: "terminal" },
  processes: { title: "进程监控", kicker: "PROCESS MONITOR", description: "查看正在运行的进程，按内存占用排序。", icon: "activity" },
  audit: { title: "操作审计", kicker: "ACTIVITY & AUDIT", description: "每一次登录与管理操作，都有迹可循。", icon: "shield" },
  info: { title: "主机信息", kicker: "HOST INFORMATION", description: "了解当前环境、运行配置与工作台能力。", icon: "server" },
};
const state = { csrf: "", session: null, metrics: null, audit: [], view: "overview", generation: 0, path: "", entries: [], containers: [], processes: [], filter: "", editor: null, commands: [], historyIndex: 0, terminal: "Harbor diagnostics · 输入 help 查看可用命令。\n这是诊断命令工作区，不提供交互式 Shell。\n\n", polling: false, refreshing: false };
let toastTimer;
let filesRequest = 0;
let editorRequest = 0;
let terminalBusy = false;

function bytes(value, digits = 1) {
  let number = Number(value) || 0;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit++; }
  return `${number.toFixed(unit ? digits : 0)} ${units[unit]}`;
}

function duration(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds % 86400 / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return `${days ? `${days} 天 ` : ""}${hours} 小时 ${minutes} 分钟`;
}

function timestamp(value) {
  return new Date(value * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").classList.toggle("is-error", error);
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 7000 : 3500);
}

function showLogin() {
  state.csrf = "";
  state.session = null;
  state.generation++;
  state.editor = null;
  state.metrics = null;
  state.entries = [];
  state.containers = [];
  state.processes = [];
  state.audit = [];
  state.commands = [];
  state.terminal = "Harbor diagnostics · 输入 help 查看可用命令。\n这是诊断命令工作区，不提供交互式 Shell。\n\n";
  $("editor-content").value = "";
  $("output-content").textContent = "";
  $("app").hidden = true;
  $("boot").hidden = true;
  $("login").hidden = false;
  refreshAuthForm().catch((error) => { $("login-error").textContent = error.message; });
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
}

async function refreshAuthForm() {
  const setup = await api("setup", { login: true });
  $("registration-fields").hidden = !setup.required;
  $("login-fields").hidden = setup.required;
  $("auth-title").textContent = setup.required ? "创建管理员账户" : "登录你的工作台";
  $("auth-description").textContent = setup.required ? "使用安装时提供的一次性凭证完成首次注册。" : "使用管理员账户登录。";
  $("auth-submit").innerHTML = setup.required ? '创建账户 <span aria-hidden="true">↗</span>' : '进入工作台 <span aria-hidden="true">↗</span>';
  $("login-hint").textContent = setup.required ? "凭证仅可使用一次，注册后立即失效。" : "首次运行请先完成管理员注册。";
  $("password").required = !setup.required;
  $("username").required = !setup.required;
  $("credential").required = setup.required;
  $("new-username").required = setup.required;
  $("new-password").required = setup.required;
  $("confirm-password").required = setup.required;
  $("login-form").dataset.setup = String(setup.required);
}

async function api(path, options = {}) {
  const session = state.session;
  const { method = "GET", body, login = false } = options;
  const headers = { Accept: "application/json" };
  if (method !== "GET") { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = state.csrf; }
  const response = await fetch(`/api/${path}`, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`服务器返回了无效响应（HTTP ${response.status}）。`); }
  if (!login && session !== state.session) throw new Error("会话已更改，请重新操作。");
  if (!response.ok) {
    if (response.status === 401 && !login) showLogin();
    throw new Error(data.error || `请求失败（HTTP ${response.status}）。`);
  }
  return data;
}

function renderNav() {
  $("nav").innerHTML = Object.entries(views).map(([key, view], index) => `${index === 5 ? '<div class="nav-divider"></div><span class="nav-caption">管理与记录</span>' : ""}<a class="nav-link" data-nav="${key}" href="#${key}">${icon(view.icon)}${view.title}</a>`).join("");
}

function setConnection(online) {
  const badge = $("connection-badge");
  badge.className = `badge ${online ? "green" : "amber"}`;
  badge.innerHTML = `<i class="status-dot"></i> ${online ? "实时连接" : "连接中断"}`;
}

function empty(title, description, symbol = "folder") {
  return `<div class="empty-state">${icon(symbol)}<h3>${escapeHTML(title)}</h3><p>${escapeHTML(description)}</p></div>`;
}

function viewError(error) {
  return `<section class="panel">${empty("暂时无法读取数据", error.message, "server")}<div class="dialog-footer"><span class="muted">请确认本机权限与对应服务状态。</span><button class="button secondary" data-retry>重新加载</button></div></section>`;
}

async function navigate() {
  if (!state.session) return;
  const requested = location.hash.slice(1);
  state.view = Object.hasOwn(views, requested) ? requested : "overview";
  const view = views[state.view];
  const generation = ++state.generation;
  $("page-title").textContent = view.title;
  $("page-label").textContent = view.title;
  $("page-kicker").textContent = view.kicker;
  $("page-description").textContent = view.description;
  document.title = `${view.title} · Harbor Panel`;
  document.querySelectorAll("[data-nav]").forEach((item) => {
    item.classList.toggle("active", item.dataset.nav === state.view);
    if (item.dataset.nav === state.view) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  closeNav();
  $("view").innerHTML = '<div class="loading">正在读取主机数据…</div>';
  try {
    if (state.view === "overview") {
      const [metrics, audit] = await Promise.all([api("metrics"), api("audit")]);
      if (generation !== state.generation) return;
      state.metrics = metrics;
      state.audit = audit.events;
      renderOverview();
    } else if (state.view === "files") {
      await loadFiles(state.path, generation);
    } else if (state.view === "containers") {
      const data = await api("containers");
      if (generation !== state.generation) return;
      state.containers = data.containers;
      renderContainers();
    } else if (state.view === "terminal") {
      renderTerminal();
    } else if (state.view === "processes") {
      const data = await api("processes");
      if (generation !== state.generation) return;
      state.processes = data.processes;
      renderProcesses(data.total);
    } else if (state.view === "audit") {
      const data = await api("audit");
      if (generation !== state.generation) return;
      state.audit = data.events;
      renderAudit();
    } else {
      state.metrics = await api("metrics");
      if (generation !== state.generation) return;
      renderInfo();
    }
  } catch (error) {
    if (generation === state.generation && state.session) $("view").innerHTML = viewError(error);
  }
}

function chart(history) {
  if (history.length < 2) return empty("正在建立监控曲线", "每 2 秒采样一次，稍后即可看到真实趋势。", "activity");
  const first = history[0].time;
  const span = Math.max(history[history.length - 1].time - first, 1);
  const points = (key) => history.map((point) => `${(40 + (point.time - first) / span * 560).toFixed(1)},${(170 - Math.min(100, Math.max(0, point[key])) * 1.5).toFixed(1)}`).join(" ");
  return `<svg class="chart" viewBox="0 0 620 208" role="img" aria-label="CPU 和内存使用率的实际采样趋势">${[0, 25, 50, 75, 100].map((value) => `<line class="grid-line" x1="40" y1="${170 - value * 1.5}" x2="600" y2="${170 - value * 1.5}"/><text x="0" y="${174 - value * 1.5}">${value}%</text>`).join("")}<polygon points="40,170 ${points("cpu")} 600,170" fill="#77b397" fill-opacity=".08"/><polyline points="${points("memory")}" fill="none" stroke="#b7abd1" stroke-width="2" stroke-linejoin="round"/><polyline points="${points("cpu")}" fill="none" stroke="#76ab8e" stroke-width="2.5" stroke-linejoin="round"/>${[0, .25, .5, .75, 1].map((ratio) => `<text x="${40 + ratio * 560}" y="201" text-anchor="${ratio === 0 ? "start" : ratio === 1 ? "end" : "middle"}">${new Date((first + span * ratio) * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</text>`).join("")}</svg>`;
}

function statCard(title, symbol, value, unit, left, right, percent) {
  return `<section class="panel stat-card"><div class="stat-top"><span class="stat-title">${title}</span><span class="stat-symbol">${icon(symbol)}</span></div><div class="stat-value">${value}<small>${unit}</small></div><div class="stat-meta"><span>${left}</span><strong>${right}</strong></div><progress max="100" value="${percent}" aria-label="${title}"></progress></section>`;
}

function detail(label, value, mono = false) {
  return `<div><dt>${escapeHTML(label)}</dt><dd${mono ? ' class="mono"' : ""}>${escapeHTML(value)}</dd></div>`;
}

function renderOverview() {
  const m = state.metrics;
  if (!m) return;
  const focusedHref = document.activeElement.closest("#view a")?.getAttribute("href");
  const totalRate = m.network.received + m.network.sent;
  const rateParts = bytes(totalRate).split(" ");
  $("view").innerHTML = `<section class="hero"><div class="hero-copy"><p class="eyebrow">A GOOD DAY TO KEEP THINGS RUNNING</p><h2>让每一台服务器，都井然有序。</h2><p>欢迎回到你的工作台。${escapeHTML(m.hostname)} 的运行状态，正在实时同步。</p><div class="hero-pills"><span>${icon("server")}${escapeHTML(m.platform)} · ${m.cpu_count} 核心</span><span>${icon("clock")}已运行 ${duration(m.uptime)}</span><span>${icon("shield")}管理员会话已验证</span></div></div><div class="hero-illustration" aria-hidden="true"><svg viewBox="0 0 190 150" fill="none"><ellipse cx="94" cy="132" rx="75" ry="8" fill="#b7cba5" opacity=".35"/><path d="m44 27 65-13 48 22-64 14-49-23Z" fill="#d9e7c8" stroke="#9fb38b"/><path d="M44 27v26l49 23V50L44 27Z" fill="#c4d7b0" stroke="#9fb38b"/><path d="m93 50 64-14v26L93 76V50Z" fill="#eff4e3" stroke="#9fb38b"/><path d="m44 67 49 23 64-14v26l-64 14-49-23V67Z" fill="#d6e4c5" stroke="#9fb38b"/><path d="m44 67 16-4 33 16 47-10 17 7-64 14-49-23Z" fill="#e5edda" stroke="#9fb38b"/><path d="M93 90v26m-37-34 22 10m-22-51 22 10" stroke="#9db288"/><circle cx="144" cy="51" r="3" fill="#7d9c64"/><circle cx="143" cy="92" r="3" fill="#7d9c64"/><path d="M26 125V98m0 14c-13-1-16-12-16-12s14-1 16 12Zm0-7c12-1 13-14 13-14s-13 2-13 14Z" stroke="#83a06e" stroke-width="2"/><path d="m159 16 5-7m-14 6-2-8m16 17 8-1" stroke="#b2c499" stroke-width="2"/></svg></div></section>
    <div class="stats-grid">${statCard("CPU 使用率", "cpu", m.cpu_percent.toFixed(1), "%", `${m.cpu_count} 个逻辑核心`, m.cpu_percent > 85 ? "负载较高" : "实时采样", m.cpu_percent)}${statCard("内存使用率", "memory", m.memory.percent.toFixed(1), "%", `${bytes(m.memory.used)} / ${bytes(m.memory.total)}`, "物理内存", m.memory.percent)}${statCard("磁盘使用率", "disk", m.disk.percent.toFixed(1), "%", `已用 ${bytes(m.disk.used)}`, `共 ${bytes(m.disk.total)}`, m.disk.percent)}<section class="panel stat-card"><div class="stat-top"><span class="stat-title">实时网络速率</span><span class="stat-symbol">${icon("network")}</span></div><div class="stat-value">${rateParts[0]}<small>${rateParts[1]}/s</small></div><div class="stat-meta"><span>所有网卡累计吞吐</span><strong>↑↓ 实时</strong></div><div class="net-mini"><span>↓ ${bytes(m.network.received)}/s</span><span>↑ ${bytes(m.network.sent)}/s</span></div></section></div>
    <div class="dashboard-grid"><section class="panel"><div class="panel-heading"><div><h2>资源使用趋势</h2><p>当前会话 · 最多保留最近 3 分钟采样</p></div><div class="legend"><span><i></i>CPU</span><span><i class="memory-key"></i>内存</span></div></div><div class="chart-wrap">${chart(m.history)}</div></section><section class="panel"><div class="panel-heading"><h2>主机信息</h2><a class="button ghost small" href="#info">详情 ${icon("arrow")}</a></div><dl class="host-details">${detail("主机名称", m.hostname, true)}${detail("操作系统", `${m.platform} ${m.release}`)}${detail("系统架构", m.architecture)}${detail("逻辑核心", `${m.cpu_count} cores`)}${detail("开机时间", timestamp(Date.now() / 1000 - m.uptime))}${detail("文件卷剩余", bytes(m.disk.free))}</dl></section></div>
    <div class="overview-bottom"><section class="panel quick-panel"><div class="panel-heading"><div><h2>常用工作区</h2><p>从这里，继续你的日常工作。</p></div>${icon("grid")}</div><div class="shortcut-grid">${["files", "terminal", "containers", "processes"].map((key) => `<a class="shortcut" href="#${key}">${icon(views[key].icon)}<span><strong>${views[key].title}</strong><small>${{ files: "浏览和编辑文件", terminal: "快速诊断主机", containers: "查看与管理容器", processes: "了解资源占用" }[key]}</small></span>${icon("arrow", "arrow")}</a>`).join("")}</div></section><section class="panel"><div class="panel-heading"><h2>最近活动</h2><a class="button ghost small" href="#audit">全部 ${icon("arrow")}</a></div><div class="audit-list">${state.audit.filter((event) => !event.action.endsWith(".requested")).slice(0, 4).map((event) => `<div class="audit-row">${icon(event.success ? "shield" : "activity")}<span class="audit-name">${escapeHTML(auditName(event.action))}</span><time>${new Date(event.time * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</time></div>`).join("") || '<p class="muted">还没有操作记录。</p>'}</div></section></div>`;
  if (focusedHref) [...$("view").querySelectorAll("a")].find((link) => link.getAttribute("href") === focusedHref)?.focus({ preventScroll: true });
}

async function loadFiles(path, generation = state.generation) {
  const request = ++filesRequest;
  const data = await api(`files?path=${encodeURIComponent(path)}`);
  if (state.view !== "files" || generation !== state.generation || request !== filesRequest) return;
  state.path = data.path;
  state.entries = data.entries;
  renderFiles(data.truncated);
}

function renderFiles(truncated) {
  const segments = state.path ? state.path.split("/") : [];
  const breadcrumbs = `<button data-folder="">${icon("folder")} 文件根目录</button>${segments.map((name, index) => `<span>/</span><button data-folder="${escapeHTML(segments.slice(0, index + 1).join("/"))}">${escapeHTML(name)}</button>`).join("")}`;
  $("view").innerHTML = `<div class="section-note">管理范围：<span class="mono">${escapeHTML(state.session.file_root)}</span>。文本编辑与上传上限 256 KiB，下载上限 8 MiB，符号链接不可访问。</div><section class="panel"><div class="toolbar"><div class="folder-path">${breadcrumbs}</div><div class="toolbar-group"><button class="button secondary small" data-new-folder>${icon("plus")}文件夹</button><button class="button secondary small" data-upload>${icon("upload")}上传文本</button><input id="file-upload" type="file" hidden><button class="button primary small" data-new-file>${icon("plus")}新建文件</button></div></div><div class="table-scroll"><table><thead><tr><th>名称</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead><tbody>${state.path ? `<tr><td colspan="4"><button class="text-button" data-folder="${escapeHTML(segments.slice(0, -1).join("/"))}">↰ 返回上级目录</button></td></tr>` : ""}${state.entries.map((entry, index) => `<tr><td><div class="entry-name">${icon(entry.directory ? "folder" : "file", entry.directory ? "" : "file-icon")}${entry.linked ? `<span>${escapeHTML(entry.name)} <span class="badge neutral">链接 · 不可访问</span></span>` : `<button class="text-button" data-entry="${index}" title="${escapeHTML(entry.name)}">${escapeHTML(entry.name)}</button>`}</div></td><td class="mono cell-secondary">${entry.directory ? "—" : bytes(entry.size)}</td><td class="cell-secondary">${timestamp(entry.modified)}</td><td>${!entry.directory && !entry.linked ? `<a class="button ghost small" href="/api/files/download?path=${encodeURIComponent(joinPath(entry.name))}" download>${icon("download")}下载</a>` : "—"}</td></tr>`).join("")}</tbody></table></div>${state.entries.length ? "" : empty("这里还很安静", "新建文件或上传 UTF-8 文本，开始整理工作目录。")}<div class="table-footer"><span>${state.entries.length} 个项目${truncated ? " · 目录过大，仅显示前 2,000 项" : ""}</span><span>原子保存 · 版本冲突检查</span></div></section>`;
}

function joinPath(name) { return state.path ? `${state.path}/${name}` : name; }

function openEditor(path, content = "", etag = null) {
  state.editor = { path, content, etag };
  $("editor-title").textContent = etag === null ? "新建文本文件" : "编辑文件";
  $("editor-name").value = path;
  $("editor-name").readOnly = etag !== null;
  $("editor-content").value = content;
  $("editor-status").textContent = "保存时检查文件版本，避免覆盖外部修改。";
  $("editor-dialog").showModal();
  (etag === null ? $("editor-name") : $("editor-content")).focus();
}

function closeEditor() {
  if (state.editor && ($("editor-content").value !== state.editor.content || $("editor-name").value !== state.editor.path) && !confirm("尚有未保存的修改，确定关闭？")) return;
  $("editor-dialog").close();
  state.editor = null;
}

async function saveEditor() {
  if (!state.editor || $("editor-save").disabled) return;
  const editor = state.editor;
  const path = $("editor-name").value.trim();
  if (!path || path.endsWith("/")) { toast("请输入完整的文件路径。", true); return; }
  const button = $("editor-save");
  button.disabled = true;
  try {
    const content = $("editor-content").value;
    const result = await api("files/write", { method: "PUT", body: { path, content, etag: editor.etag } });
    if (state.editor !== editor) return;
    state.editor = { path, content, etag: result.etag };
    $("editor-name").readOnly = true;
    $("editor-title").textContent = "编辑文件";
    $("editor-status").textContent = "文件已保存。";
    toast("文件已保存。");
    if (state.view === "files") await loadFiles(state.path);
  } catch (error) { $("editor-status").textContent = error.message; toast(error.message, true); }
  finally { button.disabled = false; }
}

async function uploadFile(file) {
  if (!file) return;
  const folder = state.path;
  const path = joinPath(file.name);
  const session = state.session;
  if (file.size > 256 * 1024) { toast("仅支持上传 256 KiB 以内的 UTF-8 文本。", true); return; }
  let content;
  try { content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()); }
  catch { toast("文件必须是有效的 UTF-8 文本。", true); return; }
  if (session !== state.session) return;
  try {
    await api("files/write", { method: "PUT", body: { path, content, etag: null } });
    toast("文本文件已上传。");
    if (state.view === "files" && state.path === folder) await loadFiles(folder);
  } catch (error) { toast(error.message, true); }
}

function renderContainers() {
  const running = state.containers.filter((item) => item.state === "running").length;
  $("view").innerHTML = `<div class="section-note">连接本机 Docker CLI 与当前 Docker context。启停操作会影响真实容器，请确认目标后执行。</div><section class="panel"><div class="toolbar"><div class="toolbar-group">${icon("box")}<strong>本机容器</strong><span class="badge green">${running} 运行中</span><span class="badge neutral">共 ${state.containers.length} 个</span></div><button class="button secondary small" data-retry>${icon("refresh")}同步容器</button></div>${state.containers.length ? `<div class="table-scroll"><table><thead><tr><th>容器 / 镜像</th><th>状态</th><th>端口</th><th>操作</th></tr></thead><tbody>${state.containers.map((item, index) => `<tr><td><strong>${escapeHTML(item.name)}</strong><p class="cell-secondary mono" title="${escapeHTML(item.image)}">${escapeHTML(item.image)}</p><span class="cell-secondary mono">${escapeHTML(item.id.slice(0, 12))}</span></td><td><span class="badge ${item.state === "running" ? "green" : "neutral"}">${escapeHTML(item.state)}</span><p class="cell-secondary">${escapeHTML(item.status)}</p></td><td class="cell-secondary mono">${escapeHTML(item.ports || "未发布端口")}</td><td><div class="actions"><button class="button ghost small" data-container="${index}" data-action="logs">日志</button>${item.state === "running" ? `<button class="button secondary small" data-container="${index}" data-action="restart">重启</button><button class="button danger small" data-container="${index}" data-action="stop">停止</button>` : `<button class="button secondary small" data-container="${index}" data-action="start">启动</button>`}</div></td></tr>`).join("")}</tbody></table></div>` : empty("还没有容器", "Docker 已连接，目前没有发现容器。", "box")}</section>`;
}

async function containerAction(index, action, button) {
  const generation = state.generation;
  const item = state.containers[index];
  if (!item) return;
  if (action !== "logs" && !confirm(`确定${{ start: "启动", stop: "停止", restart: "重启" }[action]}容器 ${item.name}？此操作将影响真实服务。`)) return;
  button.disabled = true;
  try {
    if (action === "logs") {
      const result = await api(`containers/logs?id=${encodeURIComponent(item.id)}`);
      if (generation !== state.generation) return;
      $("output-title").textContent = `${item.name} · 最近 150 行日志`;
      $("output-content").textContent = result.output || "容器暂无日志。";
      $("output-dialog").showModal();
    } else {
      await api("containers/action", { method: "POST", body: { id: item.id, action } });
      toast("容器操作已完成。");
      if (state.view === "containers") await navigate();
    }
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function renderTerminal() {
  $("view").innerHTML = `<div class="terminal-layout"><section class="terminal-window"><div class="terminal-titlebar"><span class="terminal-dots"><i></i><i></i><i></i></span><span>diagnostics @ ${escapeHTML(state.metrics?.hostname || "localhost")}</span><span>只读诊断</span></div><pre id="terminal-output" role="log" aria-label="诊断输出" tabindex="0"></pre><form id="terminal-form" class="terminal-input"><label for="terminal-command">❯</label><input id="terminal-command" autocomplete="off" spellcheck="false" placeholder="输入命令，例如 uptime"><button class="button primary small" type="submit">执行</button></form></section><aside class="panel terminal-help"><h3>常用诊断命令</h3><p>点击命令即可执行。这里只运行预设诊断，不支持 Shell、管道、脚本或系统修改。</p><div class="commands">${["help", "hostname", "whoami", "uptime", "df -h", "free -h", "ps", "docker ps"].map((command) => `<button class="command-chip" data-command="${command}">${command}<span>↵</span></button>`).join("")}</div><p>使用 ↑ / ↓ 查看本次会话的命令历史。</p></aside></div>`;
  $("terminal-output").textContent = state.terminal;
  $("terminal-output").scrollTop = $("terminal-output").scrollHeight;
}

async function runCommand(command) {
  if (!command || terminalBusy || !$("terminal-form")) return;
  terminalBusy = true;
  const session = state.session;
  const form = $("terminal-form");
  form.dataset.busy = "true";
  form.querySelector("button").disabled = true;
  state.commands.push(command);
  state.historyIndex = state.commands.length;
  state.terminal += `❯ ${command}\n`;
  $("terminal-command").value = "";
  const update = () => { const output = $("terminal-output"); if (output) { output.textContent = state.terminal; output.scrollTop = output.scrollHeight; } };
  update();
  try {
    const result = await api("terminal", { method: "POST", body: { command } });
    state.terminal += `${result.output}\n\n`;
  } catch (error) { if (session === state.session) state.terminal += `Error: ${error.message}\n\n`; }
  finally {
    terminalBusy = false;
    state.terminal = state.terminal.slice(-100000);
    update();
    const currentForm = $("terminal-form");
    if (currentForm) { currentForm.dataset.busy = "false"; currentForm.querySelector("button").disabled = false; }
    $("terminal-command")?.focus();
  }
}

function processRows() {
  const filtered = state.processes.filter((item) => `${item.name} ${item.pid} ${item.user}`.toLowerCase().includes(state.filter.toLowerCase()));
  return filtered.map((item) => `<tr><td class="mono">${item.pid}</td><td><strong>${escapeHTML(item.name)}</strong></td><td class="cell-secondary">${escapeHTML(item.user)}</td><td><span class="badge ${["running", "sleeping", "idle"].includes(item.status) ? "green" : "neutral"}">${escapeHTML(item.status)}</span></td><td class="mono">${bytes(item.memory)}</td></tr>`).join("") || '<tr><td colspan="5">没有匹配的进程。</td></tr>';
}

function renderProcesses(total) {
  $("view").innerHTML = `<div class="section-note">只读查看进程，按常驻内存（RSS）降序显示前 150 项；每 9 秒刷新一次，无法访问的进程会被跳过。</div><section class="panel"><div class="toolbar"><div class="toolbar-group">${icon("activity")}<strong>当前可见进程</strong><span class="badge neutral" id="process-total">${total} 个</span></div><label><span class="sr-only">筛选进程</span><input id="process-search" type="search" placeholder="搜索进程名称、PID 或用户" value="${escapeHTML(state.filter)}"></label></div><div class="table-scroll"><table><thead><tr><th>PID</th><th>进程名称</th><th>用户</th><th>状态</th><th>常驻内存 ↓</th></tr></thead><tbody id="process-rows">${processRows()}</tbody></table></div><div class="table-footer"><span>显示前 ${state.processes.length} 个可访问进程</span><span>只读监控</span></div></section>`;
}

function auditName(action) {
  const names = { login: "管理员登录", logout: "管理员退出", terminal: "执行诊断", "files.write": "保存文件", "files.mkdir": "创建文件夹", "containers.action": "容器管理" };
  const base = action.replace(/\.requested$/, "");
  return `${names[base] || base}${action.endsWith(".requested") ? " · 请求" : ""}`;
}

function renderAudit() {
  $("view").innerHTML = `<div class="section-note">显示最近 100 条记录，本地保留最近 10,000 条。记录操作意图与结果，不记录密码或文件正文。</div><section class="panel"><div class="toolbar"><strong>操作记录</strong><span class="badge neutral">${state.audit.length} 条</span></div><div class="table-scroll"><table><thead><tr><th>时间</th><th>操作</th><th>目标</th><th>来源</th><th>结果</th></tr></thead><tbody>${state.audit.map((event) => `<tr><td class="mono cell-secondary">${timestamp(event.time)}</td><td>${escapeHTML(auditName(event.action))}</td><td class="cell-secondary mono" title="${escapeHTML(event.target)}">${escapeHTML(event.target)}</td><td class="mono cell-secondary">${escapeHTML(event.address)}</td><td><span class="badge ${event.action.endsWith(".requested") ? "neutral" : event.success ? "green" : "red"}">${event.action.endsWith(".requested") ? "已请求" : event.success ? "成功" : "失败"}</span></td></tr>`).join("")}</tbody></table></div>${state.audit.length ? "" : empty("还没有操作记录", "登录和管理操作将显示在这里。", "shield")}</section>`;
}

function renderInfo() {
  const m = state.metrics;
  $("view").innerHTML = `<div class="info-grid"><section class="panel"><h2>当前主机</h2><dl class="host-details">${detail("主机名称", m.hostname, true)}${detail("系统", `${m.platform} ${m.release}`)}${detail("处理器", m.cpu_name)}${detail("架构 / 核心", `${m.architecture} / ${m.cpu_count}`)}${detail("物理内存", bytes(m.memory.total))}${detail("系统负载", m.load ? m.load.map((value) => value.toFixed(2)).join(" / ") : "此平台不提供 load average")}${detail("累计接收 / 发送", `${bytes(m.network.received_total)} / ${bytes(m.network.sent_total)}`)}</dl></section><section class="panel"><h2>工作台配置</h2><dl class="host-details">${detail("版本", `Harbor Panel ${state.session.version}`)}${detail("管理模式", "单主机 · 单管理员")}${detail("文件根目录", state.session.file_root, true)}${detail("会话有效期", "8 小时，服务重启后失效")}${detail("监控采样", "2 秒 / 内存保留 90 个点")}${detail("文件编辑", "UTF-8 / 256 KiB / 版本检查")}${detail("会话与请求保护", "HttpOnly Cookie / SameSite / CSRF")}</dl></section><section class="panel info-wide"><h2>从这里继续扩展</h2><p>这是独立实现的核心运维版本，所有已显示指标来自当前运行主机。Docker 需要本机安装 CLI 并能够访问对应 daemon；面板进程只能使用其操作系统账户已有的权限。</p><ul><li>当前支持：监控、进程查看、目录浏览、UTF-8 文件创建/编辑/上传、文件下载、容器启停/重启/日志、诊断与审计。</li><li>尚未实现：远程节点、网站与证书管理、应用市场、交互式 SSH / PTY、AI 助手和桌面窗口系统。</li><li>首次运行需使用一次性凭证注册管理员账户；部署时可通过 --root 指定文件目录。外部访问应配置 HTTPS 反向代理和 --public-origin。</li></ul><p>完整启动、测试与 Linux systemd 部署说明见项目目录中的 README.md。</p></section></div>`;
}

function searchViews() {
  const query = $("search-input").value.trim().toLowerCase();
  const aliases = { overview: "cpu memory dashboard 监控", files: "file upload 编辑 上传", containers: "docker logs 容器 日志", terminal: "shell command 诊断 命令", processes: "process pid 进程", audit: "audit log 审计 记录", info: "setting settings 配置 信息" };
  const results = Object.entries(views).filter(([key, view]) => `${view.title} ${aliases[key]}`.toLowerCase().includes(query));
  $("search-results").innerHTML = results.map(([key, view]) => `<a class="search-result" href="#${key}" data-search-result>${icon(view.icon)}<div>${view.title}<small>${view.description}</small></div></a>`).join("") || empty("没有匹配的功能", "试试搜索「文件」「Docker」或「终端」。", "search");
}

function closeNav() { $("sidebar").classList.remove("is-open"); $("nav-overlay").hidden = true; }

let pollCount = 0;
async function poll() {
  if (!state.session || state.polling || document.hidden) return;
  state.polling = true;
  const generation = state.generation;
  try {
    const metrics = await api("metrics");
    if (!state.session) return;
    state.metrics = metrics;
    $("sidebar-host").textContent = metrics.hostname;
    $("last-updated").textContent = `最近同步 ${timestamp(metrics.sampled_at)}`;
    setConnection(true);
    if (state.view === "overview" && generation === state.generation) renderOverview();
    pollCount++;
    if (state.view === "processes" && pollCount % 3 === 0) {
      const data = await api("processes");
      if (generation === state.generation && $("process-rows")) {
        state.processes = data.processes;
        $("process-rows").innerHTML = processRows();
        $("process-total").textContent = `${data.total} 个`;
      }
    }
  } catch { if (state.session) setConnection(false); }
  finally { state.polling = false; }
}

async function enterApp() {
  state.session = await api("session");
  state.csrf = state.session.csrf;
  $("login").hidden = true;
  $("boot").hidden = true;
  $("app").hidden = false;
  await navigate();
  await poll();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    const setup = event.currentTarget.dataset.setup === "true";
    const body = setup
      ? { credential: $("credential").value, username: $("new-username").value, password: $("new-password").value, confirmation: $("confirm-password").value }
      : { username: $("username").value, password: $("password").value };
    if (setup && body.password !== body.confirmation) throw new Error("两次输入的密码不一致。");
    await api(setup ? "register" : "login", { method: "POST", login: true, body });
    ["credential", "new-password", "confirm-password", "password"].forEach((id) => { $(id).value = ""; });
    $("password").value = "";
    state.path = "";
    await enterApp();
  } catch (error) { showLogin(); $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("logout", { method: "POST", body: {} });
    state.editor = null;
    state.metrics = null;
    state.terminal = "Harbor diagnostics · 输入 help 查看可用命令。\n\n";
    state.commands = [];
    showLogin();
  } catch (error) { toast(error.message, true); }
});

$("refresh").addEventListener("click", async () => {
  if (state.refreshing) return;
  state.refreshing = true;
  $("refresh").disabled = true;
  try { await navigate(); }
  finally { state.refreshing = false; $("refresh").disabled = false; }
});
$("menu-button").addEventListener("click", () => { $("sidebar").classList.add("is-open"); $("nav-overlay").hidden = false; });
$("nav-overlay").addEventListener("click", closeNav);
$("open-search").addEventListener("click", () => { $("search-input").value = ""; searchViews(); $("search-dialog").showModal(); $("search-input").focus(); });
$("search-input").addEventListener("input", searchViews);
$("editor-save").addEventListener("click", saveEditor);
$("editor-close").addEventListener("click", closeEditor);
$("editor-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeEditor(); });
$("folder-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("folder-name").value.trim();
  if (!name || /[/\\:]/.test(name) || name === "." || name === "..") { toast("请输入有效的单层文件夹名称。", true); return; }
  const button = event.currentTarget.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    await api("files/mkdir", { method: "POST", body: { path: joinPath(name) } });
    $("folder-dialog").close();
    toast("文件夹已创建。");
    if (state.view === "files") await loadFiles(state.path);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button, a");
  if (!target) return;
  try {
    if (target.dataset.close) $(target.dataset.close).close();
    if (target.hasAttribute("data-search-result")) $("search-dialog").close();
    if (target.hasAttribute("data-retry")) await navigate();
    if (target.hasAttribute("data-folder")) await loadFiles(target.dataset.folder);
    if (target.hasAttribute("data-entry")) {
      const generation = state.generation;
      const request = ++editorRequest;
      const entry = state.entries[Number(target.dataset.entry)];
      if (entry.directory) await loadFiles(joinPath(entry.name));
      else {
        const file = await api(`files/read?path=${encodeURIComponent(joinPath(entry.name))}`);
        if (generation === state.generation && request === editorRequest) openEditor(file.path, file.content, file.etag);
      }
    }
    if (target.hasAttribute("data-new-file")) openEditor(joinPath(""));
    if (target.hasAttribute("data-new-folder")) { $("folder-name").value = ""; $("folder-dialog").showModal(); }
    if (target.hasAttribute("data-upload")) $("file-upload").click();
    if (target.hasAttribute("data-container")) await containerAction(Number(target.dataset.container), target.dataset.action, target);
    if (target.hasAttribute("data-command")) await runCommand(target.dataset.command);
  } catch (error) { toast(error.message, true); }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "file-upload") uploadFile(event.target.files[0]);
});
document.addEventListener("input", (event) => {
  if (event.target.id === "process-search") { state.filter = event.target.value; $("process-rows").innerHTML = processRows(); }
});
document.addEventListener("submit", (event) => {
  if (event.target.id === "terminal-form") { event.preventDefault(); runCommand($("terminal-command").value.trim()); }
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && state.session) {
    event.preventDefault();
    if (!document.querySelector("dialog[open]")) $("open-search").click();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && $("editor-dialog").open) { event.preventDefault(); saveEditor(); }
  if (event.target.id === "terminal-command" && ["ArrowUp", "ArrowDown"].includes(event.key)) {
    event.preventDefault();
    state.historyIndex = Math.max(0, Math.min(state.commands.length, state.historyIndex + (event.key === "ArrowUp" ? -1 : 1)));
    event.target.value = state.commands[state.historyIndex] || "";
  }
});
window.addEventListener("beforeunload", (event) => {
  if (state.editor && ($("editor-content").value !== state.editor.content || $("editor-name").value !== state.editor.path)) { event.preventDefault(); event.returnValue = ""; }
});
window.addEventListener("hashchange", navigate);
renderNav();
enterApp().catch((error) => { showLogin(); if (!error.message.includes("sign in")) $("login-error").textContent = error.message; });
setInterval(poll, 3000);
