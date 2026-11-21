"use strict";

const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const icon = (name, extra = "") => `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const views = {
  overview: { title: "主机概览", kicker: "WORKSPACE OVERVIEW", description: "一切尽在掌握，开始今天的运维。", icon: "grid" },
  files: { title: "文件管理", kicker: "FILE WORKSPACE", description: "浏览、创建和编辑文件，让每一份配置各就其位。", icon: "folder" },
  containers: { title: "Docker 容器", kicker: "CONTAINER MANAGEMENT", description: "连接本机 Docker，管理容器生命周期与日志。", icon: "box" },
  terminal: { title: "终端", kicker: "INTERACTIVE SSH", description: "通过 SSH 直接操作本机 VPS，或安全连接其他服务器。", icon: "terminal" },
  processes: { title: "进程监控", kicker: "PROCESS MONITOR", description: "查看正在运行的进程，按内存占用排序。", icon: "activity" },
  audit: { title: "操作审计", kicker: "ACTIVITY & AUDIT", description: "每一次登录与管理操作，都有迹可循。", icon: "shield" },
  appearance: { title: "外观主题", kicker: "YOUR VISUAL SPACE", description: "选择有纹理的配色主题，也可以上传自己的背景图片。", icon: "sun" },
  info: { title: "主机信息", kicker: "HOST INFORMATION", description: "了解当前环境、运行配置与工作台能力。", icon: "server" },
};
const state = { csrf: "", session: null, appearance: { theme: "botanical", has_background: false }, metrics: null, audit: [], view: "overview", generation: 0, path: "", entries: [], containers: [], processes: [], filter: "", editor: null, polling: false, refreshing: false };
let toastTimer;
let filesRequest = 0;
let editorRequest = 0;
let terminalEmulator = null;
let terminalFit = null;
let terminalSocket = null;
let terminalResizeObserver = null;
let terminalConnectRequest = null;
let appearanceBusy = false;

function disposeTerminal() {
  if (terminalResizeObserver) terminalResizeObserver.disconnect();
  terminalResizeObserver = null;
  if (terminalSocket) {
    terminalSocket.onopen = null;
    terminalSocket.onmessage = null;
    terminalSocket.onclose = null;
    terminalSocket.onerror = null;
    terminalSocket.close(1000, "Terminal view closed");
  }
  terminalSocket = null;
  terminalConnectRequest = null;
  if (terminalEmulator) terminalEmulator.dispose();
  terminalEmulator = null;
  terminalFit = null;
}

function applyAppearance() {
  document.documentElement.dataset.theme = state.appearance.theme || "botanical";
  const image = state.appearance.has_background
    ? `url("/api/appearance/background?v=${Date.now()}")`
    : "none";
  document.documentElement.style.setProperty("--wallpaper-image", image);
}

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
  disposeTerminal();
  state.csrf = "";
  state.session = null;
  state.appearance = { theme: "botanical", has_background: false };
  applyAppearance();
  state.generation++;
  state.editor = null;
  state.metrics = null;
  state.entries = [];
  state.containers = [];
  state.processes = [];
  state.audit = [];
  $("editor-content").value = "";
  $("output-content").textContent = "";
  $("app").hidden = true;
  $("boot").hidden = true;
  $("login").hidden = false;
  $("security-warning")?.remove();
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
  const warnings = [];
  if (setup.full_host_root) warnings.push("此原生服务以 root 运行，管理员可完全控制 VPS，包括系统文件、Docker 和命令执行。仅在信任的环境使用。");
  if (setup.public_http) warnings.push("当前可通过公网 HTTP 访问：连接未加密，密码和登录会话可能被窃听。建议限制来源 IP，或配置 Nginx 与 HTTPS。");
  const hint = setup.required ? "凭证仅可使用一次，注册后立即失效。" : "请使用已注册的管理员账户登录。";
  $("login-hint").textContent = [...warnings, hint].join(" ");
  $("login-hint").classList.toggle("is-warning", warnings.length > 0);
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
    const error = new Error(data.error || `请求失败（HTTP ${response.status}）。`);
    error.code = data.code;
    throw error;
  }
  return data;
}

function renderNav() {
  $("nav").innerHTML = Object.entries(views).map(([key, view], index) => `${index === 5 ? '<div class="nav-divider"></div><span class="nav-caption">管理与记录</span>' : ""}<a class="nav-link" data-nav="${key}" href="#${key}">${icon(view.icon)}${view.title}</a>`).join("");
}

function setConnection(online) {
  const badge = $("connection-badge");
  badge.className = `badge ${online ? "green" : "amber"}`;
  badge.innerHTML = `<i class="status-dot"></i> ${online ? "面板已连接" : "面板连接中断"}`;
}

function empty(title, description, symbol = "folder") {
  return `<div class="empty-state">${icon(symbol)}<h3>${escapeHTML(title)}</h3><p>${escapeHTML(description)}</p></div>`;
}

function viewError(error) {
  if (state.view === "containers") {
    const dockerErrors = {
      docker_socket_permission: ["Docker 权限不足", "面板进程无法访问当前 Docker socket。原生 root 部署仍无法连接时，请检查 Docker daemon 是否运行、CLI 与 context 是否可用，以及现有服务配置是否将进程降权；Compose 模式默认不挂载宿主机 Docker socket。"],
      docker_cli_missing: ["未找到 Docker CLI", "原生部署请先安装 Docker CLI。默认容器镜像未启用宿主机 Docker 管理。"],
      docker_daemon_unavailable: ["无法连接 Docker 服务", "请检查 Docker 服务是否正在运行，以及面板使用的 Docker context 是否正确。"],
    };
    const [title, description] = Object.prototype.hasOwnProperty.call(dockerErrors, error.code)
      ? dockerErrors[error.code] : ["Docker 暂不可用", error.message];
    return `<section class="panel">${empty(title, description, "box")}<div class="dialog-footer"><button class="button secondary" data-retry>重新加载</button></div></section>`;
  }
  return `<section class="panel">${empty("暂时无法读取数据", error.message, "server")}<div class="dialog-footer"><span class="muted">请确认本机权限与对应服务状态。</span><button class="button secondary" data-retry>重新加载</button></div></section>`;
}

async function navigate() {
  if (!state.session) return;
  const requested = location.hash.slice(1);
  const nextView = Object.hasOwn(views, requested) ? requested : "overview";
  if (state.view === "terminal") disposeTerminal();
  state.view = nextView;
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
  $("view").setAttribute("aria-live", state.view === "terminal" ? "off" : "polite");
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
    } else if (state.view === "appearance") {
      const appearance = await api("appearance");
      if (generation !== state.generation) return;
      if (!appearanceBusy) state.appearance = appearance;
      applyAppearance();
      renderAppearance();
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
    <div class="overview-bottom"><section class="panel quick-panel"><div class="panel-heading"><div><h2>常用工作区</h2><p>从这里，继续你的日常工作。</p></div>${icon("grid")}</div><div class="shortcut-grid">${["files", "terminal", "containers", "processes"].map((key) => `<a class="shortcut" href="#${key}">${icon(views[key].icon)}<span><strong>${views[key].title}</strong><small>${{ files: "浏览和编辑文件", terminal: "打开 SSH 终端", containers: "查看与管理容器", processes: "了解资源占用" }[key]}</small></span>${icon("arrow", "arrow")}</a>`).join("")}</div></section><section class="panel"><div class="panel-heading"><h2>最近活动</h2><a class="button ghost small" href="#audit">全部 ${icon("arrow")}</a></div><div class="audit-list">${state.audit.filter((event) => !event.action.endsWith(".requested")).slice(0, 4).map((event) => `<div class="audit-row">${icon(event.success ? "shield" : "activity")}<span class="audit-name">${escapeHTML(auditName(event.action))}</span><time>${new Date(event.time * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</time></div>`).join("") || '<p class="muted">还没有操作记录。</p>'}</div></section></div>`;
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
  const canFollowLinks = state.session.full_host;
  const linkCopy = canFollowLinks ? "符号链接仅跟随根目录内的目标。" : "符号链接不可访问。";
  const entries = state.entries.map((entry, index) => {
    const unavailable = entry.linked && !canFollowLinks;
    const name = unavailable
      ? `<span>${escapeHTML(entry.name)} <span class="badge neutral">链接 · 不可访问</span></span>`
      : `<button class="text-button" data-entry="${index}" title="${escapeHTML(entry.name)}">${escapeHTML(entry.name)}${entry.linked ? ' <span class="badge neutral">符号链接</span>' : ""}</button>`;
    const download = !entry.directory && !unavailable
      ? `<a class="button ghost small" href="/api/files/download?path=${encodeURIComponent(joinPath(entry.name))}" download>${icon("download")}下载</a>`
      : "—";
    return `<tr><td><div class="entry-name">${icon(entry.directory ? "folder" : "file", entry.directory ? "" : "file-icon")}${name}</div></td><td class="mono cell-secondary">${entry.directory ? "—" : bytes(entry.size)}</td><td class="cell-secondary">${timestamp(entry.modified)}</td><td>${download}</td></tr>`;
  }).join("");
  $("view").innerHTML = `<div class="section-note">管理范围：<span class="mono">${escapeHTML(state.session.file_root)}</span>。文本编辑与上传上限 256 KiB，下载上限 8 MiB。${linkCopy}</div><section class="panel"><div class="toolbar"><div class="folder-path">${breadcrumbs}</div><div class="toolbar-group"><button class="button secondary small" data-new-folder>${icon("plus")}文件夹</button><button class="button secondary small" data-upload>${icon("upload")}上传文本</button><input id="file-upload" type="file" hidden><button class="button primary small" data-new-file>${icon("plus")}新建文件</button></div></div><div class="table-scroll"><table><thead><tr><th>名称</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead><tbody>${state.path ? `<tr><td colspan="4"><button class="text-button" data-folder="${escapeHTML(segments.slice(0, -1).join("/"))}">↰ 返回上级目录</button></td></tr>` : ""}${entries}</tbody></table></div>${state.entries.length ? "" : empty("这里还很安静", "新建文件或上传 UTF-8 文本，开始整理工作目录。")}<div class="table-footer"><span>${state.entries.length} 个项目${truncated ? " · 目录过大，仅显示前 2,000 项" : ""}</span><span>原子保存 · 版本冲突检查</span></div></section>`;
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
  $("view").innerHTML = `<div class="section-note">连接本机 Docker CLI 与当前 Docker context。启停操作会影响真实容器，请确认目标后执行。</div><section class="panel"><div class="toolbar"><div class="toolbar-group">${icon("box")}<strong>本机容器</strong><span class="badge green">${running} 运行中</span><span class="badge neutral">共 ${state.containers.length} 个</span></div><button class="button secondary small" data-retry>${icon("refresh")}同步容器</button></div>${state.containers.length ? `<div class="table-scroll"><table><thead><tr><th>容器 / 镜像</th><th>状态</th><th>端口</th><th>操作</th></tr></thead><tbody>${state.containers.map((item, index) => `<tr><td><strong>${escapeHTML(item.name)}</strong><p class="cell-secondary mono" title="${escapeHTML(item.image)}">${escapeHTML(item.image)}</p><span class="cell-secondary mono">${escapeHTML(item.id.slice(0, 12))}</span></td><td><span class="badge ${item.state === "running" ? "green" : "neutral"}">${escapeHTML(item.state)}</span><p class="cell-secondary">${escapeHTML(item.status)}</p></td><td class="cell-secondary mono">${escapeHTML(item.ports || "未发布端口")}</td><td><div class="actions"><button class="button ghost small" data-container="${index}" data-action="config">编辑</button><button class="button ghost small" data-container="${index}" data-action="logs">日志</button>${item.state === "running" ? `<button class="button secondary small" data-container="${index}" data-action="restart">重启</button><button class="button danger small" data-container="${index}" data-action="stop">停止</button>` : `<button class="button secondary small" data-container="${index}" data-action="start">启动</button>`}</div></td></tr>`).join("")}</tbody></table></div>` : empty("还没有容器", "Docker 已连接，目前没有发现容器。", "box")}</section>`;
}

function lines(value) { return Array.isArray(value) ? value.join("\n") : ""; }

async function openContainerConfig(index) {
  const item = state.containers[index];
  if (!item) return;
  const generation = state.generation;
  const data = await api(`containers/config?id=${encodeURIComponent(item.id)}`);
  if (generation !== state.generation) return;
  const config = data.config;
  $("container-config-id").value = data.id;
  $("container-config-name").value = config.name || "";
  $("container-config-image").value = config.image || "";
  $("container-config-command").value = lines(config.command);
  $("container-config-ports").value = lines(config.ports);
  $("container-config-environment").value = lines(config.environment);
  $("container-config-volumes").value = lines(config.volumes);
  $("container-config-networks").value = lines(config.networks);
  $("container-config-restart").value = config.restart_policy || "no";
  $("container-config-memory").value = config.memory_limit || 0;
  $("container-config-cpu").value = config.cpu_limit || 0;
  $("container-config-title").textContent = `${config.name || item.name} · 容器配置`;
  $("container-config-dialog").showModal();
}

function listField(id) { return $(id).value.split("\n").map((value) => value.trim()).filter(Boolean); }

async function saveContainerConfig(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]');
  const body = { id: $("container-config-id").value, config: {
    name: $("container-config-name").value.trim(), image: $("container-config-image").value.trim(),
    command: listField("container-config-command"), ports: listField("container-config-ports"),
    environment: listField("container-config-environment"), volumes: listField("container-config-volumes"),
    networks: listField("container-config-networks"), restart_policy: $("container-config-restart").value,
    memory_limit: Number($("container-config-memory").value || 0), cpu_limit: Number($("container-config-cpu").value || 0),
  }};
  if (!confirm("保存将停止并重建容器，容器 ID 会改变。挂载卷/目录中的数据通常会保留，未挂载的可写层数据可能丢失；如果创建失败，面板会尝试恢复旧容器。继续？")) return;
  button.disabled = true;
  try {
    await api("containers/config", { method: "PUT", body });
    $("container-config-dialog").close();
    toast("容器已按新配置重建。\n");
    if (state.view === "containers") await navigate();
  } catch (error) { toast(`重建失败：${error.message}\n如果页面仍显示旧容器，请刷新列表确认状态。`, true); }
  finally { button.disabled = false; }
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

function terminalStatus(text, error = false) {
  const status = $("terminal-status");
  if (!status) return;
  status.textContent = text;
  status.classList.toggle("is-error", error);
}

function terminalControl(message) {
  if (terminalSocket?.readyState === WebSocket.OPEN) terminalSocket.send(JSON.stringify(message));
}

function sendTerminalConnect(request) {
  if (!terminalSocket || terminalSocket.readyState !== WebSocket.OPEN || !request) return;
  const dimensions = terminalFit?.proposeDimensions() || { cols: 100, rows: 28 };
  terminalControl({ type: "connect", ...request, ...dimensions });
  terminalConnectRequest = null;
  if (request.target === "remote") { request.password = null; if ($("remote-password")) $("remote-password").value = ""; }
  terminalStatus(request.target === "local" ? "正在连接本机 SSH…" : `正在连接 ${request.host}…`);
}

function connectTerminal(target, remote = null) {
  if (state.view !== "terminal" || !terminalEmulator) return;
  terminalConnectRequest = target === "local" ? { target: "local" } : { target: "remote", ...remote };
  if (terminalSocket?.readyState === WebSocket.OPEN) {
    sendTerminalConnect(terminalConnectRequest);
    return;
  }
  if (terminalSocket && terminalSocket.readyState === WebSocket.CONNECTING) return;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${scheme}//${location.host}/api/terminal/ws`, [state.csrf]);
  const emulator = terminalEmulator;
  terminalSocket = socket;
  const current = () => terminalSocket === socket && terminalEmulator === emulator;
  socket.binaryType = "arraybuffer";
  socket.onopen = () => { if (current()) sendTerminalConnect(terminalConnectRequest); };
  socket.onmessage = (event) => {
    if (!current()) return;
    if (typeof event.data !== "string") {
      if (terminalEmulator) terminalEmulator.write(new Uint8Array(event.data));
      return;
    }
    let message;
    try { message = JSON.parse(event.data); }
    catch { terminalStatus("收到无效的终端控制消息。", true); return; }
    if (message.type === "host_key") {
      const accepted = window.confirm(`首次连接 SSH 主机 ${message.host}（${message.key_type}）\n指纹：${message.fingerprint}\n\n请通过 VPS 控制台或其他可信渠道核对指纹。仅在确认匹配后继续。`);
      terminalControl({ type: "host_key", challenge: message.challenge, accepted });
    } else if (message.type === "connected") {
      terminalStatus(`已连接 ${message.username}@${message.host}`);
      terminalEmulator?.focus();
    } else if (message.type === "error") {
      terminalStatus(message.message || "SSH 连接失败。", true);
      terminalEmulator?.write(`\r\n\x1b[31m${message.message || "SSH 连接失败。"}\x1b[0m\r\n`);
    } else if (message.type === "disconnected") {
      terminalStatus(message.message || "SSH 已断开。");
      terminalEmulator?.write(`\r\n\x1b[90m${message.message || "SSH 已断开。"}\x1b[0m\r\n`);
    }
  };
  socket.onerror = () => { if (current()) terminalStatus("WebSocket 连接失败，请检查面板地址和代理配置。", true); };
  socket.onclose = (event) => {
    if (!current()) return;
    terminalConnectRequest = null;
    terminalStatus(event.reason || "终端连接已关闭。", event.code !== 1000);
  };
}

function terminalInput(data) {
  if (terminalSocket?.readyState !== WebSocket.OPEN) return;
  for (let offset = 0; offset < data.length; offset += 16384) terminalSocket.send(data.subarray(offset, offset + 16384));
}

function renderTerminal() {
  const remoteAllowed = Boolean(state.session?.https_remote_ssh);
  $("view").innerHTML = `<div class="terminal-layout"><section class="terminal-window"><div class="terminal-titlebar"><span class="terminal-dots"><i></i><i></i><i></i></span><span class="terminal-title">SSH · ${escapeHTML(state.metrics?.hostname || "本机")}</span><span id="terminal-status" role="status">准备连接本机 SSH…</span></div><div id="terminal-emulator" class="terminal-emulator" role="application" aria-label="SSH 交互终端"></div><div class="terminal-toolbar"><button class="button secondary small" data-terminal-action="local">连接本机</button><button class="button secondary small" data-terminal-action="disconnect">断开 SSH</button></div></section><aside class="panel terminal-help"><h2>连接其他 VPS</h2><p>远程连接使用 SSH root 用户和密码，不会保存密码。首次连接会显示主机指纹；请在可信渠道核实后再接受。</p><form id="remote-ssh-form" class="remote-ssh-form"><label for="remote-host">主机名或公网 IP</label><input id="remote-host" required maxlength="253" autocomplete="url" placeholder="例如 203.0.113.10"><label for="remote-port">SSH 端口</label><input id="remote-port" type="number" min="1" max="65535" value="22" required><label for="remote-user">SSH 用户</label><input id="remote-user" value="root" readonly aria-readonly="true"><label for="remote-password">root 密码</label><input id="remote-password" type="password" autocomplete="current-password" required maxlength="1024" ${remoteAllowed ? "" : "disabled"}><button class="button primary" type="submit" ${remoteAllowed ? "" : "disabled"}>连接远程 VPS</button></form>${remoteAllowed ? "" : '<p class="terminal-warning">远程密码 SSH 仅在使用已配置的 HTTPS 面板地址时可用。当前可以使用本机 SSH。</p>'}<p class="terminal-note">原生 root 部署使用仅监听回环地址的独立 SSH 服务自动连接；无需输入本机密码，也不修改系统 SSH 配置或 root 的 authorized_keys。退出此页面、退出登录或关闭浏览器时，SSH 会话将断开。</p></aside></div>`;
  try {
    terminalEmulator = new window.Terminal({ cursorBlink: true, scrollback: 5000, fontSize: 13, fontFamily: '"Cascadia Code",Consolas,monospace', theme: { background: "#14221f", foreground: "#dce9dc", cursor: "#c2df9d", selectionBackground: "#59826c88" } });
    terminalFit = new window.FitAddon.FitAddon();
    terminalEmulator.loadAddon(terminalFit);
    terminalEmulator.open($("terminal-emulator"));
    terminalFit.fit();
    terminalEmulator.onData((data) => {
      terminalInput(new TextEncoder().encode(data));
    });
    terminalEmulator.onBinary((data) => {
      terminalInput(Uint8Array.from(data, (char) => char.charCodeAt(0) & 255));
    });
    terminalResizeObserver = new ResizeObserver(() => {
      if (!terminalEmulator || !terminalFit) return;
      terminalFit.fit();
      const dimensions = terminalFit.proposeDimensions();
      if (dimensions) terminalControl({ type: "resize", ...dimensions });
    });
    terminalResizeObserver.observe($("terminal-emulator"));
    terminalStatus("正在连接本机 SSH…");
    connectTerminal("local");
  } catch (error) {
    disposeTerminal();
    terminalStatus(`终端初始化失败：${error.message}`, true);
  }
}

const themeNames = { botanical: "苔影花园", topography: "等高线", nocturne: "月下档案", terracotta: "陶土织纹" };
const themeDescriptions = { botanical: "蕨叶轮廓与雾林绿 · 平静、清透", topography: "地图等高线与矿物青 · 灵动、层叠", nocturne: "星图轨迹与靛蓝纸 · 深邃、克制", terracotta: "陶土花窗与奶油砂岩 · 温润、沉静" };

function renderAppearance() {
  const imageUrl = state.appearance.has_background ? `/api/appearance/background?v=${Date.now()}` : "";
  $("view").innerHTML = `<section class="appearance-intro"><span class="appearance-orbit orbit-one"></span><span class="appearance-orbit orbit-two"></span><p class="eyebrow">A LITTLE MORE YOU</p><h2>让工作空间，也有自己的气息。</h2><p>每个主题都有独立的图案与色彩。上传图片仅保存于本机面板状态目录，不会传到外部服务。</p></section><div class="theme-grid">${Object.entries(themeNames).map(([id, name]) => `<button class="theme-card ${state.appearance.theme === id ? "is-selected" : ""}" data-theme-pick="${id}" data-preview-theme="${id}" aria-pressed="${state.appearance.theme === id}"><span class="theme-swatch"><i></i><i></i><i></i><i></i></span><span class="theme-card-copy"><strong>${name}</strong><small>${themeDescriptions[id]}</small></span><span class="theme-check">${state.appearance.theme === id ? "✓" : ""}</span></button>`).join("")}</div><section class="panel wallpaper-panel"><div class="wallpaper-preview ${imageUrl ? "has-wallpaper" : ""}" ${imageUrl ? `style="background-image:linear-gradient(120deg,#172d2999,#172d2966),url('${imageUrl}')"` : ""}><span class="wallpaper-preview-mark">${icon("sun")}</span><span>${imageUrl ? "当前自定义背景" : "背景预览"}</span></div><div class="wallpaper-controls"><div><h2>上传背景图片</h2><p>支持 PNG、JPEG、WebP，最大 1.5 MB（1,500,000 字节）、800 万像素；图片会在本机优化并移除元数据。</p><p class="muted">背景作为轻柔纹理呈现，不会干扰面板卡片内容。</p></div><div class="wallpaper-actions"><input id="background-upload" type="file" accept="image/png,image/jpeg,image/webp" hidden><button class="button primary" data-background-upload>${icon("upload")}选择图片</button>${imageUrl ? '<button class="button secondary" data-background-remove>移除背景</button>' : ""}</div></div></section>`;
}

async function uploadBackground(file) {
  if (!file) return;
  const allowed = ["image/png", "image/jpeg", "image/webp"];
  if (!allowed.includes(file.type) || file.size > 1_500_000) { toast("请选择不超过 1.5 MB（1,500,000 字节） 的 PNG、JPEG 或 WebP 图片。", true); return; }
  await changeAppearance(async () => {
  const session = state.session;
  const response = await fetch("/api/appearance/background", { method: "PUT", credentials: "same-origin", headers: { "X-CSRF-Token": state.csrf, "X-Image-Type": file.type }, body: file });
  let result;
  try { result = await response.json(); }
  catch { throw new Error(`服务器返回了无效响应（HTTP ${response.status}）。`); }
  if (session !== state.session) throw new Error("会话已更改，请重新操作。");
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(result.error || `上传失败（HTTP ${response.status}）。`);
  }
  return result;
  }, "背景图片已在本机安全保存。");
}

async function changeAppearance(operation, message) {
  if (appearanceBusy) return;
  appearanceBusy = true;
  const session = state.session;
  document.querySelectorAll("[data-theme-pick], [data-background-upload], [data-background-remove]").forEach((button) => { button.disabled = true; });
  try {
    const appearance = await operation();
    if (session !== state.session) return;
    state.appearance = appearance;
    applyAppearance();
    toast(message);
  } finally {
    appearanceBusy = false;
    if (session === state.session && state.view === "appearance") renderAppearance();
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
  const names = { login: "管理员登录", logout: "管理员退出", terminal: "执行命令", "ssh.connect": "SSH 连接", "ssh.disconnect": "SSH 断开", "files.write": "保存文件", "files.mkdir": "创建文件夹", "containers.action": "容器管理", "appearance.background": "更换背景", appearance: "更换主题" };
  const base = action.replace(/\.requested$/, "");
  return `${names[base] || base}${action.endsWith(".requested") ? " · 请求" : ""}`;
}

function renderAudit() {
  $("view").innerHTML = `<div class="section-note">显示最近 100 条记录，本地保留最近 10,000 条。记录操作意图与结果，不记录密码或文件正文。</div><section class="panel"><div class="toolbar"><strong>操作记录</strong><span class="badge neutral">${state.audit.length} 条</span></div><div class="table-scroll"><table><thead><tr><th>时间</th><th>操作</th><th>目标</th><th>来源</th><th>结果</th></tr></thead><tbody>${state.audit.map((event) => `<tr><td class="mono cell-secondary">${timestamp(event.time)}</td><td>${escapeHTML(auditName(event.action))}</td><td class="cell-secondary mono" title="${escapeHTML(event.target)}">${escapeHTML(event.target)}</td><td class="mono cell-secondary">${escapeHTML(event.address)}</td><td><span class="badge ${event.action.endsWith(".requested") ? "neutral" : event.success ? "green" : "red"}">${event.action.endsWith(".requested") ? "已请求" : event.success ? "成功" : "失败"}</span></td></tr>`).join("")}</tbody></table></div>${state.audit.length ? "" : empty("还没有操作记录", "登录和管理操作将显示在这里。", "shield")}</section>`;
}

function renderInfo() {
  const m = state.metrics;
  const accessMode = state.session.full_host_root ? "全主机 root · 单管理员" : state.session.full_host ? "全主机 · 服务账户权限" : "受限文件目录 · 单管理员";
  const terminalDescription = "交互式终端通过独立的本机 SSH 服务自动连接原生 root VPS；安装器会安装 OpenSSH，无需手动配置密钥。远程 root 密码 SSH 仅通过已配置 HTTPS 面板地址开放。";
  const proxyAdvice = state.session.public_http
    ? "公网 HTTP 未加密；限制来源 IP，或自行配置 Nginx 与 HTTPS。"
    : "需要域名访问时可自行配置 Nginx 与 HTTPS 反向代理。";
  $("view").innerHTML = `<div class="info-grid"><section class="panel"><h2>当前主机</h2><dl class="host-details">${detail("主机名称", m.hostname, true)}${detail("系统", `${m.platform} ${m.release}`)}${detail("处理器", m.cpu_name)}${detail("架构 / 核心", `${m.architecture} / ${m.cpu_count}`)}${detail("物理内存", bytes(m.memory.total))}${detail("系统负载", m.load ? m.load.map((value) => value.toFixed(2)).join(" / ") : "此平台不提供 load average")}${detail("累计接收 / 发送", `${bytes(m.network.received_total)} / ${bytes(m.network.sent_total)}`)}</dl></section><section class="panel"><h2>工作台配置</h2><dl class="host-details">${detail("版本", `Harbor Panel ${state.session.version}`)}${detail("管理模式", accessMode)}${detail("文件根目录", state.session.file_root, true)}${detail("会话有效期", "8 小时，服务重启后失效")}${detail("监控采样", "2 秒 / 内存保留 90 个点")}${detail("文件编辑", "UTF-8 / 256 KiB / 版本检查")}${detail("会话与请求保护", "HttpOnly Cookie / SameSite / CSRF")}</dl></section><section class="panel info-wide"><h2>从这里继续扩展</h2><p>这是独立实现的核心运维版本，所有已显示指标来自当前运行主机。${terminalDescription}Docker CLI 与 daemon 需安装并运行；是否能管理容器取决于服务实际权限。${proxyAdvice}</p><ul><li>当前支持：监控、进程查看、目录浏览、UTF-8 文件创建/编辑/上传、文件下载、容器管理、SSH 终端、图案主题与本机背景图片、审计。</li><li>远程 SSH 会核对并记住首次接受的主机指纹；更换指纹时连接会被阻止。终端内容和密码不会写入审计记录。</li><li>管理员账户每次安装仅注册一次；更新保留账户，卸载后旧凭证和账户失效。</li></ul><p>完整启动、测试与 Linux systemd 部署说明见项目目录中的 README.md。</p></section></div>`;
}

function searchViews() {
  const query = $("search-input").value.trim().toLowerCase();
  const aliases = { overview: "cpu memory dashboard 监控", files: "file upload 编辑 上传", containers: "docker logs 容器 日志", terminal: "shell ssh pty 诊断 命令 远程服务器", processes: "process pid 进程", audit: "audit log 审计 记录", appearance: "theme background wallpaper 主题 外观 背景 图片", info: "setting settings 配置 信息" };
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
  const session = await api("session");
  state.session = session;
  state.csrf = session.csrf;
  const appearance = await api("appearance");
  if (state.session !== session) return;
  state.appearance = appearance;
  applyAppearance();
  if (session.full_host) {
    let warning = $("security-warning");
    if (!warning) {
      warning = document.createElement("p");
      warning.id = "security-warning";
      warning.className = "security-banner";
      warning.setAttribute("role", "alert");
      $("main").prepend(warning);
    }
    const privilegeWarning = session.full_host_root
      ? "本面板以 root 运行，可完全控制 VPS 文件、Docker 和系统命令。"
      : "此模式允许浏览全主机路径，但实际操作仅具有面板服务账户的权限。";
    warning.textContent = `高权限主机管理：${privilegeWarning}${session.public_http ? "公网 HTTP 未加密，密码和会话可能被窃听；请限制端口来源，或自行配置 Nginx 与 HTTPS。" : "请仅通过可信网络访问，并妥善保管管理员凭证。"}`;
  } else {
    $("security-warning")?.remove();
  }
  $("login").hidden = true;
  $("boot").hidden = true;
  $("app").hidden = false;
  await navigate();
  if (state.session !== session) return;
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
$("container-config-form").addEventListener("submit", saveContainerConfig);

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
    if (target.hasAttribute("data-container") && target.dataset.action === "config") await openContainerConfig(Number(target.dataset.container));
    else if (target.hasAttribute("data-container")) await containerAction(Number(target.dataset.container), target.dataset.action, target);
    if (target.hasAttribute("data-terminal-action")) {
      if (target.dataset.terminalAction === "local") connectTerminal("local");
      else if (target.dataset.terminalAction === "disconnect") {
        terminalConnectRequest = null;
        if ($("remote-password")) $("remote-password").value = "";
        terminalControl({ type: "disconnect" });
        terminalStatus("SSH 已断开。");
      }
    }
    if (target.hasAttribute("data-theme-pick")) {
      await changeAppearance(() => api("appearance", { method: "POST", body: { theme: target.dataset.themePick } }), `已切换至「${themeNames[target.dataset.themePick]}」。`);
    }
    if (target.hasAttribute("data-background-upload")) $("background-upload").click();
    if (target.hasAttribute("data-background-remove")) {
      await changeAppearance(() => api("appearance/background/remove", { method: "POST", body: {} }), "已移除自定义背景。");
    }
  } catch (error) { toast(error.message, true); }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "file-upload") uploadFile(event.target.files[0]);
  if (event.target.id === "background-upload") uploadBackground(event.target.files[0]).catch((error) => toast(error.message, true));
});
document.addEventListener("input", (event) => {
  if (event.target.id === "process-search") { state.filter = event.target.value; $("process-rows").innerHTML = processRows(); }
});
document.addEventListener("submit", (event) => {
  if (event.target.id === "remote-ssh-form") {
    event.preventDefault();
    if (!state.session?.https_remote_ssh) { toast("远程密码 SSH 仅在 HTTPS 面板地址下可用。", true); return; }
    const password = $("remote-password").value;
    if (!password) { toast("请输入远程 VPS 的 root 密码。", true); return; }
    connectTerminal("remote", { host: $("remote-host").value.trim(), port: Number($("remote-port").value), username: "root", password });
    $("remote-password").value = "";
  }
});
document.addEventListener("keydown", (event) => {
  if (event.target.closest?.(".xterm")) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && state.session) {
    event.preventDefault();
    if (!document.querySelector("dialog[open]")) $("open-search").click();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && $("editor-dialog").open) { event.preventDefault(); saveEditor(); }
});
window.addEventListener("beforeunload", (event) => {
  if (state.editor && ($("editor-content").value !== state.editor.content || $("editor-name").value !== state.editor.path)) { event.preventDefault(); event.returnValue = ""; }
});
window.addEventListener("pagehide", disposeTerminal);
window.addEventListener("pageshow", (event) => { if (event.persisted && state.view === "terminal" && state.session) navigate(); });
window.addEventListener("hashchange", navigate);
renderNav();
enterApp().catch((error) => { showLogin(); if (!error.message.includes("sign in")) $("login-error").textContent = error.message; });
setInterval(poll, 3000);
