"use strict";

window.HarborSecuritySettings = (() => {
  function el(tag, text, attributes = {}) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    return node;
  }

  function button(text, primary = false, submit = false) {
    return el("button", text, { type: submit ? "submit" : "button", class: `button ${primary ? "primary" : "secondary"}` });
  }

  function field(form, label, properties) {
    const wrapper = el("label", label);
    const input = el("input", "", { required: "", ...properties });
    wrapper.append(input);
    form.append(wrapper);
    return input;
  }

  function decode(value) {
    return Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), (char) => char.charCodeAt(0));
  }

  function encode(buffer) {
    if (buffer == null) return null;
    return btoa(Array.from(new Uint8Array(buffer), (byte) => String.fromCharCode(byte)).join("")).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function credentialSupported() {
    return window.isSecureContext && window.PublicKeyCredential && navigator.credentials;
  }

  async function passkey(options, create, signal) {
    const parser = create ? "parseCreationOptionsFromJSON" : "parseRequestOptionsFromJSON";
    let publicKey;
    if (PublicKeyCredential[parser]) publicKey = PublicKeyCredential[parser](options);
    else {
      publicKey = { ...options, challenge: decode(options.challenge) };
      if (create) publicKey.user = { ...options.user, id: decode(options.user.id) };
      const list = create ? "excludeCredentials" : "allowCredentials";
      if (options[list]) publicKey[list] = options[list].map((item) => ({ ...item, id: decode(item.id) }));
    }
    const credential = await navigator.credentials[create ? "create" : "get"]({ publicKey, signal });
    if (credential.toJSON) return credential.toJSON();
    const response = credential.response;
    return {
      id: credential.id, rawId: encode(credential.rawId), type: credential.type,
      response: create
        ? { clientDataJSON: encode(response.clientDataJSON), attestationObject: encode(response.attestationObject) }
        : { clientDataJSON: encode(response.clientDataJSON), authenticatorData: encode(response.authenticatorData), signature: encode(response.signature), userHandle: encode(response.userHandle) },
    };
  }

  function verification(container, { api, endpoint, flow, onSuccess, onCancel, login = false }) {
    const controller = new AbortController();
    const post = (action, body) => api(`${endpoint}/${action}`, { method: "POST", login, signal: controller.signal, body: { challenge: flow.challenge, ...body } });
    const error = el("p", "", { class: "error-text", role: "alert" });
    const form = el("form", "", { class: "stack" });
    const cancel = button("返回");
    cancel.onclick = () => { controller.abort(); onCancel(); };
    let verifying = false;
    let options;
    let code, codeButton, keyButton;
    const update = () => {
      if (codeButton) codeButton.disabled = verifying;
      if (keyButton) keyButton.disabled = verifying || !options;
    };
    const handleError = (failure) => {
      if (controller.signal.aborted) return;
      if (failure.code === "mfa_restart") onCancel(failure.message);
      else error.textContent = failure.name === "NotAllowedError" ? "Passkey 操作已取消或超时。" : failure.message;
    };
    if (flow.methods.includes("totp")) {
      code = field(form, "Google Authenticator 验证码", { inputmode: "numeric", pattern: "[0-9]{6}", maxlength: "6", autocomplete: "one-time-code" });
      codeButton = button("验证", true, true);
      form.append(codeButton);
      form.onsubmit = (event) => {
        event.preventDefault();
        if (verifying) return;
        const value = code.value;
        code.value = "";
        verify(() => post("verify", { method: "totp", code: value }));
      };
    }
    const preparePasskey = async () => {
      try { options = await post("options", {}); }
      catch (failure) { handleError(failure); }
      update();
    };
    if (flow.methods.includes("passkey")) {
      if (credentialSupported()) {
        keyButton = button("使用 Passkey", !codeButton);
        keyButton.onclick = () => {
          if (verifying || !options) return;
          verify(async () => {
            const credential = await passkey(options, false, controller.signal);
            options = undefined;
            return post("verify", { method: "passkey", credential });
          }).then((success) => {
            if (!success && !options && !controller.signal.aborted) preparePasskey();
          });
        };
        preparePasskey();
      } else {
        form.append(el("p", "Passkey 需要安全上下文：HTTPS 域名或通过 SSH 隧道访问的 http://localhost。公网 IP 的 HTTP 页面受浏览器限制，无法使用。", { class: "muted" }));
      }
    }
    async function verify(act) {
      verifying = true;
      error.textContent = "";
      update();
      try {
        const result = await act();
        if (!controller.signal.aborted) await onSuccess(result);
        return true;
      } catch (failure) {
        handleError(failure);
        return false;
      } finally {
        verifying = false;
        update();
      }
    }
    if (keyButton) form.append(keyButton);
    form.append(cancel, error);
    update();
    container.replaceChildren(form);
    (code || keyButton || cancel).focus();
    return () => controller.abort();
  }

  async function mount(container, { api, signal, onSignedOut }) {
    const section = el("section", "", { class: "panel settings-card" });
    const stage = el("div", "", { class: "stack" });
    let stopVerification;
    signal.addEventListener("abort", () => { stopVerification?.(); stage.replaceChildren(); }, { once: true });
    section.append(el("h2", "登录安全"), el("p", "可为管理员密码添加 Passkey 或 Google Authenticator。两种都启用时，每次登录可任选一种验证。"), stage);
    container.replaceChildren(section);
    const status = await api("mfa", { signal });
    if (signal.aborted) return;
    const post = (action, body) => api(`mfa/${action}`, { method: "POST", body, signal });
    const error = el("p", "", { class: "error-text", role: "alert" });

    function summary(message = "") {
      stopVerification?.();
      stopVerification = undefined;
      if (signal.aborted) return;
      error.textContent = message;
      const content = [el("p", status.totp ? "Google Authenticator：已启用" : "Google Authenticator：未启用")];
      if (!status.available) content.push(el("p", "请使用已验证的面板地址和已注册的管理员账户管理第二重验证。", { class: "muted" }));
      if (status.available && location.protocol === "http:") content.push(el("p", "HTTP 不加密密码和验证密钥，请在可信网络或 SSH 隧道中设置。", { class: "muted" }));
      function action(text, callback) {
        const control = button(text);
        control.onclick = callback;
        content.push(control);
      }
      if (status.available) {
        if (status.totp) action("移除 Google Authenticator", () => reauth("remove", "totp"));
        else action("添加 Google Authenticator", () => reauth("enroll", "totp"));
      }
      status.passkeys.forEach((key, index) => {
        const date = new Date(key.created_at * 1000).toLocaleString();
        content.push(el("p", `Passkey ${index + 1} · ${date}${key.current_rp ? "" : "（其他域名）"}`));
        if (status.available) action(`移除 Passkey ${index + 1}`, () => reauth("remove", "passkey", key.id));
      });
      if (status.passkey_available && credentialSupported() && status.passkeys.length < 10) {
        action("添加 Passkey", () => reauth("enroll", "passkey"));
      } else if (status.available && !status.passkeys.length) {
        content.push(el("p", "Passkey 支持 HTTPS 域名或 http://localhost（可通过 SSH 隧道访问）。公网 IP 的 HTTP 页面无法调用浏览器 Passkey；可使用 Google Authenticator。", { class: "muted" }));
      }
      content.push(el("p", "更改验证方式会退出所有登录会话。请妥善保管设备；失去所有验证方式时，请联系主机管理员。", { class: "muted" }), error);
      stage.replaceChildren(...content);
    }

    function reauth(action, method, id) {
      const form = el("form", "", { class: "stack" });
      const factor = method === "totp" ? "Google Authenticator" : "Passkey";
      const verb = action === "remove" ? "移除" : "添加";
      form.append(el("p", `确认${verb} ${factor}：请先重新验证身份。`));
      const password = field(form, "管理员密码", { type: "password", autocomplete: "current-password", maxlength: "1024" });
      const submit = button("继续", true, true);
      const cancel = button("取消");
      cancel.onclick = () => summary();
      form.append(submit, cancel, error);
      error.textContent = "";
      stage.replaceChildren(form);
      password.focus();
      async function authorize(grant) {
        if (action === "remove") {
          await post("remove", { grant, method, id });
          if (!signal.aborted) onSignedOut();
          return;
        }
        const draft = await post("enroll/start", { grant, method });
        if (!signal.aborted && section.isConnected) enroll(draft, method);
      }
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (submit.disabled) return;
        submit.disabled = cancel.disabled = true;
        error.textContent = "";
        try {
          const value = password.value;
          password.value = "";
          const flow = await post("reauth", { password: value });
          if (!form.isConnected) return;
          if (flow.grant) await authorize(flow.grant);
          else stopVerification = verification(stage, {
            api, endpoint: "mfa/reauth", flow,
            onSuccess: async ({ grant }) => {
              try { await authorize(grant); } catch (failure) { summary(failure.message); }
            }, onCancel: summary,
          });
        } catch (failure) {
          error.textContent = failure.message;
        } finally {
          submit.disabled = cancel.disabled = false;
        }
      };
    }

    function enroll(draft, method) {
      const form = el("form", "", { class: "stack" });
      const enrollmentId = draft.enrollment_id;
      const cancel = button("取消");
      cancel.onclick = () => summary();
      let code;
      if (method === "totp") {
        form.append(el("p", "在 Google Authenticator 中扫描二维码，或输入下方密钥（基于时间）。完成后输入六位验证码。"));
        form.append(el("img", "", { alt: "Google Authenticator 配置二维码", src: draft.qr, width: "200", height: "200" }));
        form.append(el("p", draft.secret, { class: "mono" }));
        draft.secret = draft.qr = undefined;
        code = field(form, "六位验证码", { inputmode: "numeric", pattern: "[0-9]{6}", maxlength: "6", autocomplete: "one-time-code" });
      } else form.append(el("p", "使用此设备或安全密钥创建 Passkey，并完成设备验证。"));
      const submit = button(method === "totp" ? "确认启用" : "创建 Passkey", true, true);
      form.append(submit, cancel, error);
      stage.replaceChildren(form);
      (code || submit).focus();
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (submit.disabled) return;
        submit.disabled = cancel.disabled = true;
        error.textContent = "";
        try {
          const body = { enrollment_id: enrollmentId };
          if (code) { body.code = code.value; code.value = ""; }
          else body.credential = await passkey(draft.options, true, signal);
          if (!form.isConnected) return;
          await post("enroll/finish", body);
          if (!signal.aborted) {
            stage.replaceChildren();
            onSignedOut();
          }
        } catch (failure) {
          if (failure.code === "mfa_restart") summary(failure.message);
          else error.textContent = failure.name === "NotAllowedError" ? "Passkey 操作已取消或超时。" : failure.message;
        } finally {
          submit.disabled = cancel.disabled = false;
        }
      };
    }
    summary();
  }
  return { mount, verification };
})();