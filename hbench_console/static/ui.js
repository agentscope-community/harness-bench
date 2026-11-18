// Small dependency-free UI kit for the console: DOM builder, dialogs, drawer,
// toast messages, switch and layout helpers. Kept local so the console works
// on hosts without CDN access and without a frontend build step.

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// ------------------------------------------------------------------- icons
const ICONS = {
  results: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
  runs: '<circle cx="12" cy="12" r="9"/><path d="M10 8.5l5 3.5-5 3.5z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  compare: '<path d="M8 3v18M16 3v18M3 8h5M16 16h5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  success: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
  warning: '<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4.5M12 17.5v.5"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v6H4V6h6"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  empty: '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 2h6l1-2h5"/>',
};

export function icon(name, cls = "") {
  const span = document.createElement("span");
  span.className = `icon ${cls}`.trim();
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;
  return span;
}

// ------------------------------------------------------------------- toast
let toastHost = null;
export function toast(message, type = "info", duration) {
  if (!toastHost) {
    toastHost = h("div", { class: "toast-host", role: "status", "aria-live": "polite" });
    document.body.append(toastHost);
  }
  const node = h("div", { class: `toast toast-${type}` },
    icon(type === "error" ? "error" : type === "success" ? "success" : type === "warning" ? "warning" : "info"),
    h("span", { class: "toast-text", text: message }));
  toastHost.append(node);
  requestAnimationFrame(() => node.classList.add("show"));
  const close = () => {
    node.classList.remove("show");
    setTimeout(() => node.remove(), 200);
  };
  setTimeout(close, duration ?? (type === "error" ? 6000 : 3000));
  node.addEventListener("click", close);
}
toast.success = (m, d) => toast(m, "success", d);
toast.error = (m, d) => toast(m, "error", d);
toast.warning = (m, d) => toast(m, "warning", d);
toast.info = (m, d) => toast(m, "info", d);

// ------------------------------------------------------------------- overlays
const overlayStack = [];
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !overlayStack.length) return;
  overlayStack[overlayStack.length - 1].dismiss();
});

function mountOverlay(root, panel, dismiss) {
  const previous = document.activeElement;
  const entry = { dismiss };
  overlayStack.push(entry);
  document.body.append(root);
  document.body.classList.add("overlay-open");
  requestAnimationFrame(() => root.classList.add("show"));
  return () => {
    const index = overlayStack.indexOf(entry);
    if (index >= 0) overlayStack.splice(index, 1);
    root.classList.remove("show");
    setTimeout(() => {
      root.remove();
      if (!overlayStack.length) document.body.classList.remove("overlay-open");
    }, 180);
    if (previous && typeof previous.focus === "function") previous.focus();
  };
}

/** Generic modal. Returns { close, body, footer }. */
export function openModal({ title, content, footer, width = 480, onClose, maskClosable = true }) {
  let unmount = null;
  const close = () => { if (!unmount) return; unmount(); unmount = null; if (onClose) onClose(); };
  const body = h("div", { class: "modal-body" }, content);
  const footerNode = footer ? h("div", { class: "modal-footer" }, footer) : null;
  const panel = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title || "对话框" },
    h("div", { class: "modal-header" },
      h("div", { class: "modal-title", text: title || "" }),
      h("button", { class: "icon-btn", "aria-label": "关闭", onclick: close }, icon("close"))),
    body, footerNode);
  panel.style.width = `${width}px`;
  const root = h("div", { class: "modal-root" },
    h("div", { class: "mask", onclick: () => { if (maskClosable) close(); } }), panel);
  unmount = mountOverlay(root, panel, close);
  return { close, body, footer: footerNode, panel };
}

/**
 * Confirmation dialog that resolves to true/false.
 * `onOk` may be async; the OK button shows a loading state while it runs and
 * the dialog stays open (with the error shown) if it throws.
 */
export function confirmDialog({ title, content, okText = "确定", cancelText = "取消", danger = false,
  type = danger ? "warning" : "info", onOk } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => { if (settled) return; settled = true; resolve(value); };
    const errorSlot = h("div", {});
    const cancel = h("button", { text: cancelText, onclick: () => { modal.close(); } });
    const ok = h("button", { class: danger ? "danger solid" : "primary", text: okText,
      onclick: async () => {
        if (!onOk) { finish(true); modal.close(); return; }
        setLoading(ok, true);
        cancel.disabled = true;
        errorSlot.replaceChildren();
        try {
          await onOk();
          finish(true);
          modal.close();
        } catch (error) {
          errorSlot.replaceChildren(alertBox(error.message || String(error), "error"));
          setLoading(ok, false);
          cancel.disabled = false;
        }
      } });
    const modal = openModal({
      title: "",
      width: 440,
      content: h("div", { class: "confirm" },
        icon(type === "error" ? "error" : type === "warning" ? "warning" : "info", `confirm-icon ${type}`),
        h("div", { class: "confirm-main" },
          h("div", { class: "confirm-title", text: title || "确认操作" }),
          h("div", { class: "confirm-content" }, content),
          errorSlot)),
      footer: [cancel, ok],
      onClose: () => finish(false),
    });
    modal.panel.classList.add("modal-confirm");
    ok.focus();
  });
}

/** Right-side drawer for long details. */
export function openDrawer({ title, content, width = 640, extra }) {
  let unmount = null;
  const close = () => { if (unmount) { unmount(); unmount = null; } };
  const panel = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": title || "详情" },
    h("div", { class: "drawer-header" },
      h("div", { class: "drawer-title", text: title || "" }),
      h("div", { class: "row" }, extra || null,
        h("button", { class: "icon-btn", "aria-label": "关闭", onclick: close }, icon("close")))),
    h("div", { class: "drawer-body" }, content));
  panel.style.width = `min(${width}px, 100vw)`;
  const root = h("div", { class: "drawer-root" }, h("div", { class: "mask", onclick: close }), panel);
  unmount = mountOverlay(root, panel, close);
  return { close };
}

// ------------------------------------------------------------------- controls
export function setLoading(button, loading, text) {
  if (loading) {
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.disabled = true;
    button.classList.add("loading");
    button.replaceChildren(h("span", { class: "spin" }), text || button.dataset.label);
  } else {
    button.disabled = false;
    button.classList.remove("loading");
    button.replaceChildren(button.dataset.label || text || "");
    delete button.dataset.label;
  }
}

/** Accessible toggle switch. `onChange(next)` may be async and return false to revert. */
export function switchControl({ checked = false, disabled = false, onChange, label }) {
  const node = h("button", { type: "button", class: "switch", role: "switch",
    "aria-checked": String(Boolean(checked)), "aria-label": label || "" },
    h("span", { class: "switch-handle" }));
  const set = (value) => {
    node.setAttribute("aria-checked", String(Boolean(value)));
    node.classList.toggle("on", Boolean(value));
  };
  set(checked);
  node.disabled = disabled;
  node.addEventListener("click", async () => {
    const next = node.getAttribute("aria-checked") !== "true";
    if (!onChange) { set(next); return; }
    node.disabled = true;
    node.classList.add("busy");
    try {
      const result = await onChange(next);
      if (result !== false) set(next);
    } finally {
      node.disabled = false;
      node.classList.remove("busy");
    }
  });
  node.setChecked = set;
  return node;
}

// ------------------------------------------------------------------- display
export function alertBox(message, type = "info", title) {
  return h("div", { class: `alert alert-${type}`, role: type === "error" ? "alert" : null },
    icon(type === "error" ? "error" : type === "success" ? "success" : type === "warning" ? "warning" : "info"),
    h("div", { class: "alert-main" },
      title ? h("div", { class: "alert-title", text: title }) : null,
      h("div", { class: "alert-text" }, message)));
}

export function spinner(text = "加载中…") {
  return h("div", { class: "loading-block" }, h("span", { class: "spin lg" }), h("span", { text }));
}

export function emptyState(text, action) {
  return h("div", { class: "empty" }, icon("empty", "empty-icon"),
    h("div", { class: "empty-text", text }), action || null);
}

export function card({ title, extra, sub, flush = false, cls = "" }, ...body) {
  return h("section", { class: `card ${flush ? "flush" : ""} ${cls}`.trim() },
    title || extra ? h("div", { class: "card-header" },
      h("div", { class: "card-heading" },
        title ? h("h2", { class: "card-title" }, title) : null,
        sub ? h("div", { class: "card-sub", text: sub }) : null),
      extra ? h("div", { class: "card-extra" }, extra) : null) : null,
    h("div", { class: "card-body" }, body));
}

export function pageHeader({ title, sub, extra, back }) {
  return h("div", { class: "page-header" },
    h("div", { class: "page-heading" },
      back ? h("a", { class: "back", href: back, title: "返回", "aria-label": "返回" }, icon("back")) : null,
      h("div", {},
        h("h1", {}, title),
        sub ? h("div", { class: "page-sub" }, sub) : null)),
    extra ? h("div", { class: "page-extra" }, extra) : null);
}

export function setCrumbs(items) {
  const host = document.getElementById("crumbs");
  if (!host) return;
  host.replaceChildren(...items.flatMap((item, index) => {
    const [label, href] = Array.isArray(item) ? item : [item, null];
    const last = index === items.length - 1;
    const node = href && !last ? h("a", { href, text: label }) : h("span", { class: last ? "current" : "", text: label });
    return index ? [h("span", { class: "sep", text: "/" }), node] : [node];
  }));
}
