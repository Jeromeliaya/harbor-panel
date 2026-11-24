"use strict";

window.HarborHostSettings = (() => {
  const active = (job) => ["queued", "running", "awaiting_confirmation"].includes(job?.status);
  const labels = { queued: "已排队", running: "正在应用", awaiting_confirmation: "等待确认", succeeded: "已完成", failed: "失败", rolled_back: "已回滚", rollback_failed: "回滚失败，请使用 VPS 控制台" };

  function node(tag, text = "", attrs = {}) {
    const element = document.createElement(tag);
    element.textContent = text;
    for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
    return element;
  }

  function button(text, submit = false) {
    return node("button", text, { type: submit ? "submit" : "button", class: `button ${submit ? "primary" : "secondary"}` });
  }

  function field(form, text, attrs) {
    const label = node("label", text);
    const input = node("input", "", { required: "", ...attrs });
    label.append(input);
    form.append(label);
    return input;
  }

  async function mount(container, { api, signal }) {
    const stage = node("div", "", { class: "settings-grid" });
    const feedback = node("div", "", { class: "settings-feedback", role: "status" });
    const verification = node("div", "", { class: "settings-verification" });
    const refresh = button("刷新端口状态");
    container.replaceChildren(stage, verification, feedback, refresh);
    let snapshot, timer, busy = false, loading = false, cancelVerification, pending;

    function clearVerification() {
      cancelVerification?.();
      cancelVerification = undefined;
      verification.replaceChildren();
    }
    signal.addEventListener("abort", () => { clearTimeout(timer); clearVerification(); }, { once: true });
    const call = (path, options) => api(path, { ...options, signal: options?.signal || signal });
    const post = (path, body) => call(path, { method: "POST", body });

    function update(message = "") {
      const job = snapshot?.job;
      const children = [];
      if (job) {
        children.push(node("strong", `${job.kind === "ssh" ? "系统 SSH" : "面板"}：${job.old_port} → ${job.port} · ${labels[job.status] || job.status}`), node("p", job.message));
      }
      const direct = pending || (job?.kind === "panel" && job.mode === "direct" && (active(job) || job.status === "succeeded") ? job : null);
      if (direct && location.protocol === "http:" && Number(location.port || 80) !== direct.port) {
        const link = new URL(location.href);
        link.port = String(direct.port);
        link.hash = "settings";
        children.push(node("p", "端口切换可能中断当前页面。若原地址断开，稍后打开新端口并重新登录；若已回滚，请使用旧端口。"), node("a", "在新标签打开新端口", { href: link.href, target: "_blank", rel: "noopener noreferrer" }));
      }
      if (job?.kind === "panel" && ["https_proxy", "http_proxy"].includes(job.mode) && active(job)) {
        children.push(node("p", "代理切换时可能短暂断开。请保留当前域名，恢复后重新登录。"));
      }
      if (job?.status === "awaiting_confirmation") {
        const date = new Date(job.confirmation_deadline * 1000).toLocaleTimeString("zh-CN", { hour12: false });
        children.push(node("p", `请保留原 SSH 会话，并在 ${date} 前从 VPS 外另开一条 SSH 连接，核对主机指纹并通过新端口成功登录。未确认将自动回滚。`));
        const confirm = button("我已通过新端口登录，确认切换");
        confirm.disabled = busy || Date.now() >= job.confirmation_deadline * 1000;
        confirm.onclick = () => authorize("host-settings/ssh/confirm", { id: job.id });
        children.push(confirm);
      }
      if (message) children.push(node("p", message, { class: "error-text" }));
      feedback.replaceChildren(...children);
    }

    function render() {
      if (!snapshot) return;
      stage.replaceChildren(...["ssh", "panel"].map((kind) => {
        const info = snapshot[kind];
        const ssh = kind === "ssh";
        const proxy = ["https_proxy", "http_proxy"].includes(info.mode);
        const section = node("section", "", { class: "panel settings-card" });
        section.append(node("h2", ssh ? "系统 SSH 端口" : "面板端口"),
          node("p", `当前端口：${ssh ? info.ports.join(" / ") || "未能读取" : info.port}`),
          node("p", ssh ? "只修改外部 OpenSSH，不影响面板自带终端。IP:端口或域名访问均可操作。" : proxy ? `变更通过 Nginx 同步更新，域名地址保持 ${info.proxy_origin || info.public_origin}。` : "修改后请用新端口访问；建议先配置 HTTPS。", { class: "muted" }));
        if (!info.available) section.append(node("p", info.reason || "暂不可修改。", { class: "muted" }));
        if (!info.available || busy) return section;
        const form = node("form", "", { class: "stack" });
        const port = field(form, "新 TCP 端口", { type: "number", min: "1", max: "65535", step: "1" });
        let firewall;
        if (!proxy) {
          const label = node("label", "", { class: "settings-check" });
          firewall = node("input", "", { type: "checkbox", required: "" });
          label.append(firewall, document.createTextNode("已在云安全组和 VPS 防火墙放行新端口"));
          form.append(label);
        }
        form.append(button("验证身份并修改", true));
        form.onsubmit = (event) => {
          event.preventDefault();
          authorize("host-settings/change", { kind, port: port.valueAsNumber, firewall_ready: !!firewall?.checked });
        };
        section.append(form);
        return section;
      }));
    }

    function authorize(path, change) {
      if (busy || signal.aborted) return;
      busy = true;
      clearTimeout(timer);
      render();
      update();
      const form = node("form", "", { class: "stack" });
      form.append(node("h3", "验证管理员身份"));
      const password = field(form, "管理员密码", { type: "password", autocomplete: "current-password", maxlength: "1024" });
      if (!snapshot.panel.public_origin && location.protocol === "http:") {
        form.append(node("p", "HTTP 不会加密密码。请仅在可信网络或 SSH 隧道中继续。", { class: "muted" }));
      }
      const submit = button("继续", true), cancel = button("取消"), error = node("p", "", { class: "error-text", role: "alert" });
      form.append(submit, cancel, error);
      verification.replaceChildren(form);
      password.focus();
      const finish = (message = "") => {
        if (signal.aborted) return;
        clearVerification();
        busy = false;
        render();
        update(message);
        schedule();
      };
      cancel.onclick = () => finish();
      const apply = async (proof) => {
        if (signal.aborted) return;
        const direct = change.kind === "panel" && snapshot.panel.mode === "direct";
        if (direct) pending = change;
        try {
          snapshot = { ...snapshot, job: await post(path, { ...change, ...proof }) };
          pending = undefined;
          if (active(snapshot.job)) {
            for (const key of ["panel", "ssh"]) snapshot[key] = { ...snapshot[key], available: false, reason: "正在应用端口设置。" };
          }
          finish();
        } catch (failure) {
          if (!(failure instanceof TypeError)) pending = undefined;
          finish(failure.message);
        }
      };
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (submit.disabled) return;
        const value = password.value;
        password.value = "";
        submit.disabled = cancel.disabled = true;
        error.textContent = "";
        try {
          const flow = await post("mfa/reauth", { password: value, scope: "listener" });
          if (signal.aborted) return;
          if (flow.grant) return await apply({ grant: flow.grant });
          cancelVerification = window.HarborSecuritySettings.verification(verification, {
            api: call, endpoint: "mfa/reauth", flow,
            onSuccess: ({ grant }) => apply({ grant }), onCancel: finish,
          });
        } catch (failure) {
          if (!signal.aborted) error.textContent = failure.message;
        } finally { submit.disabled = cancel.disabled = false; }
      };
    }

    function schedule() {
      clearTimeout(timer);
      if (!signal.aborted && !busy && (active(snapshot?.job) || pending)) timer = setTimeout(load, 2500);
    }
    async function load() {
      if (signal.aborted || loading || busy) return;
      loading = refresh.disabled = true;
      try {
        const data = await call("host-settings");
        if (signal.aborted || busy) return;
        snapshot = data;
        if (pending && snapshot.job?.kind === "panel" && snapshot.job.port === pending.port) pending = undefined;
        render();
        update();
      } catch (failure) {
        if (!signal.aborted && !busy) update(failure.message || "连接中断，正在等待面板恢复。");
      } finally { loading = refresh.disabled = false; schedule(); }
    }
    refresh.onclick = load;
    await load();
  }

  return { mount };
})();
