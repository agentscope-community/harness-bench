import {
  h, icon, toast, confirmDialog, openDrawer, switchControl, setLoading, alertBox, spinner, emptyState,
  card, pageHeader, setCrumbs,
} from "./ui.js";

const app = document.getElementById("app");
const modeEl = document.getElementById("mode");
const viewerEl = document.getElementById("viewer");
const ACTIVE = new Set(["queued", "planning", "running", "summarizing", "archiving"]);
const STATUS = {
  queued: ["排队", "info"], planning: ["生成计划", "info"], running: ["运行中", "info"],
  summarizing: ["汇总中", "info"], archiving: ["归档中", "info"],
  succeeded: ["成功", "ok"], failed: ["失败", "bad"], cancelled: ["已取消", "warn"],
};
let catalog = null;
let pageTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map(d => d.msg || JSON.stringify(d)).join("；")
      : detail ? JSON.stringify(detail) : `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return data;
}

const pct = v => (v === null || v === undefined) ? "—" : `${(v * 100).toFixed(1)}%`;
const num = v => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("zh-CN");
const clock = s => s ? new Date(s).toLocaleString("zh-CN", { hour12: false }) : "—";
const heatColor = p => p === null || p === undefined ? null
  : `hsl(${Math.round(120 * Math.max(0, Math.min(1, p)))} 62% 38%)`;
const dur = s => {
  if (s === null || s === undefined) return "—";
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d} 天 ${hr} 小时`;
  if (hr) return `${hr} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s % 60} 秒`;
  return `${s} 秒`;
};

function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity)
    .filter(child => child !== null && child !== undefined && child !== false));
}

/** Re-render the current page later, only if the user is still on the same hash. */
function later(hash, render, ms) {
  clearTimeout(pageTimer);
  pageTimer = setTimeout(() => {
    if ((location.hash || "#/jobs") === hash) render().catch(error => fill(app, errorBox(error)));
  }, ms);
}

function badge(text, kind = "") { return h("span", { class: `badge ${kind}`.trim(), text }); }
function statusBadge(status) {
  const [label, kind] = STATUS[status] || [status, ""];
  return h("span", { class: `badge ${kind} ${ACTIVE.has(status) ? "pulse" : ""}`.trim() },
    h("span", { class: "dot" }), label);
}
function errorBox(error) { return alertBox(error.message || String(error), "error"); }
function link(href, text) { return h("a", { href, text }); }
function linkButton(href, text, cls = "") { return h("a", { class: `button ${cls}`.trim(), href, text }); }
function muted(text) { return h("p", { class: "muted", text }); }

function setMode(allowed) {
  modeEl.textContent = allowed ? "真实运行已授权" : "仅演练模式";
  modeEl.className = `mode ${allowed ? "live" : ""}`;
  modeEl.title = allowed ? "新建运行可调用付费模型" : "新建运行只验证计划，不调用模型";
}

async function loadCatalog(force = false) {
  if (!catalog || force) catalog = await api("/api/catalog");
  setMode(catalog.allow_real_runs);
  viewerEl.replaceChildren(catalog.viewer_url
    ? h("a", { href: catalog.viewer_url, target: "_blank", rel: "noopener" }, icon("external"), "逐题轨迹服务")
    : "");
  return catalog;
}

// ---------------------------------------------------------------- navigation
document.querySelectorAll("#menu a[data-icon]").forEach(a => a.prepend(icon(a.dataset.icon)));

function setNav(section) {
  document.querySelectorAll("#menu a").forEach(a => a.classList.toggle("active", a.dataset.nav === section));
}

const views = [
  [/^#\/jobs\/([^/]+)\/tasks$/, (m) => renderTasks(decodeURIComponent(m[1])), "jobs"],
  [/^#\/jobs\/([^/]+)$/, (m) => renderJob(decodeURIComponent(m[1])), "jobs"],
  [/^#\/jobs$/, () => renderJobs(), "jobs"],
  [/^#\/runs\/new$/, () => renderNewRun(), "new"],
  [/^#\/runs\/([^/]+)$/, (m) => renderRun(decodeURIComponent(m[1])), "runs"],
  [/^#\/runs$/, () => renderRuns(), "runs"],
  [/^#\/compare(?:\?.*)?$/, () => renderCompare(), "compare"],
];

async function route() {
  clearTimeout(pageTimer);
  const hash = location.hash || "#/jobs";
  for (const [pattern, view, section] of views) {
    const match = hash.match(pattern);
    if (!match) continue;
    setNav(section);
    fill(app, spinner());
    window.scrollTo(0, 0);
    try { await view(match); } catch (error) { fill(app, errorBox(error)); }
    return;
  }
  location.hash = "#/jobs";
}

// ------------------------------------------------------------------- helpers
function stat(label, value, tone = "") {
  return h("div", { class: `stat ${tone}`.trim() }, h("div", { class: "k", text: label, title: label }),
    h("div", { class: "v", text: value, title: String(value) }));
}
function descriptions(items) {
  return h("div", { class: "descriptions" }, items.filter(Boolean).map(([label, value]) =>
    h("div", { class: "item" }, h("span", { class: "label", text: `${label}：` }), h("span", { class: "value" }, value))));
}
function fixtureBanner() {
  return alertBox("该作业由 fixture 生成，仅用于验证界面，不代表任何真实评测结果。", "fixture", "演示数据");
}
function fixtureBadge(value) { return value ? badge("演示数据", "fixture") : null; }
function emptyRow(colspan, text) { return h("tr", {}, h("td", { colspan, class: "empty-row", text })); }
function table(headers, rows, emptyText = "暂无数据") {
  const head = headers.map(item => {
    const [text, cls] = Array.isArray(item) ? item : [item, ""];
    return h("th", { class: cls || null, text });
  });
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, head)),
    h("tbody", {}, rows.length ? rows : emptyRow(headers.length, emptyText))));
}

function progressBar(progress, large = false) {
  const known = progress && progress.percent !== null && progress.percent !== undefined;
  const percent = known ? Math.max(0, Math.min(100, progress.percent)) : 0;
  const bar = h("div", { class: "progress-fill" });
  bar.style.width = `${percent}%`;
  return h("div", { class: `progress-wrap ${percent >= 100 ? "done" : ""} ${large ? "progress-lg" : ""}`.trim() },
    h("div", { class: "progress", role: "progressbar", "aria-valuenow": String(percent),
      "aria-valuemin": "0", "aria-valuemax": "100" }, bar),
    h("span", { class: "progress-text", text: known ? `${percent.toFixed(1)}%` : "—" }));
}

function progressSummary(progress) {
  if (!progress) return muted("尚无进度数据：计划生成后才会创建 job 目录。");
  return h("div", { class: "progress-stats" },
    progressBar(progress, true),
    h("div", { class: "stats" },
      stat("已记录 / 计划", `${num(progress.recorded)} / ${num(progress.planned)}`, "primary"),
      stat("运行中", num(progress.running)),
      stat("排队", num(progress.queued)),
      stat("已用时间", dur(progress.elapsed_sec)),
      stat("每小时", progress.rate_per_hour === null ? "—" : `${num(progress.rate_per_hour)} trial`),
      stat("预计剩余", progress.eta_sec === null ? "—" : dur(progress.eta_sec))));
}

function activeTable(active) {
  if (!active || !active.length) return h("div", { class: "inline-empty", text: "当前没有正在执行的 trial。" });
  return table(["运行中的 trial", "任务", "开始时间"], active.map(item => h("tr", {},
    h("td", { class: "mono", text: item.trial }), h("td", { text: item.task }),
    h("td", { class: "nowrap", text: clock(item.started_at) }))));
}

function datasetsOf(combos) { return [...new Set(combos.map(c => c.dataset))].sort(); }
function agentsOf(combos) { return [...new Set(combos.map(c => c.agent))].sort(); }
function modelsOf(combos) { return [...new Set(combos.map(c => c.model))].sort(); }

function heatSection(dataset, combos, onPick) {
  const agents = agentsOf(combos), models = modelsOf(combos);
  const grid = h("div", { class: "heat" });
  grid.style.gridTemplateColumns = `170px repeat(${models.length}, minmax(140px, 1fr))`;
  grid.append(h("div", { class: "head corner", text: "harness \\ model" }));
  for (const model of models) grid.append(h("div", { class: "head col", text: model }));
  for (const agent of agents) {
    grid.append(h("div", { class: "head", text: agent }));
    for (const model of models) {
      const cell = combos.find(c => c.agent === agent && c.model === model);
      if (!cell || !cell.recorded) {
        grid.append(h("div", { class: "cell na" }, h("span", { text: "无数据" })));
        continue;
      }
      const color = heatColor(cell.pass_rate_planned);
      const notes = [];
      if (cell.missing) notes.push(`缺 ${cell.missing}`);
      if (cell.errors) notes.push(`错 ${cell.errors}`);
      if (cell.unscored) notes.push(`未评 ${cell.unscored}`);
      const node = h("div", { class: "cell", title: "点击查看未通过 trial", role: "button", tabindex: "0" },
        h("span", { class: "p", text: pct(cell.pass_rate_planned) }),
        h("span", { class: "d", text: `通过 ${cell.passed}/${cell.planned ?? cell.recorded}` }),
        notes.length ? h("span", { class: "d", text: notes.join(" · ") }) : null);
      if (color) node.style.background = color;
      const pick = () => onPick({ agent, model, dataset, outcome: "failed" });
      node.addEventListener("click", pick);
      node.addEventListener("keydown", (event) => { if (event.key === "Enter") pick(); });
      grid.append(node);
    }
  }
  return card({ title: dataset, sub: "通过率（计划口径），点击格子筛选下方未通过 trial",
    extra: h("span", { class: "legend" }, "0%", h("span", { class: "legend-bar" }), "100%") }, grid);
}

// ------------------------------------------------------------------- results
async function renderJobs() {
  const data = await api("/api/jobs");
  await loadCatalog();
  setCrumbs(["评测", "结果看板"]);
  const syncText = data.sync.last ? `上次同步 ${clock(data.sync.last)}` : "尚未同步";
  const sync = h("button", { onclick: async () => {
    setLoading(sync, true, "同步中…");
    try {
      await api("/api/jobs/sync", { method: "POST" });
      toast.success("证据目录同步完成");
      await renderJobs();
    } catch (error) {
      toast.error(`同步失败：${error.message}`);
      setLoading(sync, false);
    }
  } }, icon("refresh"), "同步证据目录");

  const totals = data.jobs.reduce((acc, job) => {
    acc.recorded += job.recorded_trials || 0;
    acc.planned += job.planned_trials || 0;
    acc.errors += job.errors || 0;
    acc.running += job.status === "running" ? 1 : 0;
    return acc;
  }, { recorded: 0, planned: 0, errors: 0, running: 0 });

  const rows = data.jobs.map(job => h("tr", {},
    h("td", {}, h("div", { class: "row" }, link(`#/jobs/${encodeURIComponent(job.name)}`, job.name),
      fixtureBadge(job.fixture), job.status === "running" ? statusBadge("running") : null,
      job.error ? badge("索引错误", "bad") : null)),
    h("td", {}, badge(job.origin === "run" ? "控制台" : "导入", job.origin === "run" ? "info" : "")),
    h("td", { class: "num", text: num(job.combos) }),
    h("td", { class: "num", text: `${num(job.recorded_trials)} / ${num(job.planned_trials)}` }),
    h("td", { class: "num", text: pct(job.pass_rate_planned) }),
    h("td", { class: `num ${job.errors ? "mark-fail" : ""}`, text: num(job.errors) }),
    h("td", { class: "nowrap", text: clock(job.updated_at) }),
    h("td", { class: "muted" }, job.error ? h("div", { class: "clamp", title: job.error, text: job.error }) : ""),
    h("td", { class: "actions" }, h("div", { class: "row", style: "justify-content:flex-end" },
      link(`#/jobs/${encodeURIComponent(job.name)}`, "看板"),
      link(`#/jobs/${encodeURIComponent(job.name)}/tasks`, "逐题")))));

  fill(app,
    pageHeader({ title: "评测结果", sub: `证据目录来自服务端配置；共 ${data.jobs.length} 个作业。`,
      extra: [h("span", { class: "muted small", text: syncText }), sync] }),
    data.sync.error ? alertBox(data.sync.error, "error", "同步错误") : null,
    data.jobs.length ? h("div", { class: "card" }, h("div", { class: "card-body" }, h("div", { class: "stats" },
      stat("作业数", num(data.jobs.length)),
      stat("运行中作业", num(totals.running), totals.running ? "primary" : ""),
      stat("已记录 trial", num(totals.recorded)),
      stat("计划 trial", num(totals.planned)),
      stat("异常 trial", num(totals.errors), totals.errors ? "bad" : "")))) : null,
    data.jobs.length
      ? card({ title: "作业列表", flush: true },
        table(["作业", "来源", ["组合", "num"], ["记录 / 计划", "num"], ["通过率（计划）", "num"],
          ["错误", "num"], "更新时间", "备注", ["操作", "num"]], rows))
      : card({}, emptyState("还没有结果。先运行一次评测，或把 OSS 上的作业目录同步到本机证据目录。",
        h("div", { class: "row" }, linkButton("#/runs/new", "新建运行", "primary")))),
  );
}

function comboTable(combos) {
  const cell = (v, cls = "") => h("td", { class: `num ${cls}`.trim(), text: v });
  const tokens = (combo, field) => {
    const missing = combo[field + "_missing"];
    if (combo[field] === null || combo[field] === undefined) return "未知";
    return `${num(combo[field])}${missing ? `（已记录小计，缺 ${missing} 题）` : ""}`;
  };
  return table(
    ["Harness", "Model", "Dataset",
      ...["计划", "记录", "已评分", "通过", "未通过", "异常", "运行中", "排队中", "未评分（无异常）", "缺失",
        "通过率（计划）", "通过率（记录）", "平均 reward", "输入 token", "输出 token"].map(x => [x, "num"])],
    combos.map(c => h("tr", {},
      h("td", { class: "nowrap", text: c.agent }), h("td", { class: "nowrap", text: c.model }),
      h("td", { class: "nowrap", text: c.dataset }),
      cell(c.planned), cell(c.recorded), cell(c.scored), cell(c.passed, "mark-pass"), cell(c.failed),
      cell(c.errors, c.errors ? "mark-fail" : ""), cell(c.running), cell(c.queued), cell(c.unscored),
      cell(c.missing, c.missing ? "mark-err" : ""),
      cell(pct(c.pass_rate_planned)), cell(pct(c.pass_rate_recorded)),
      cell(c.mean_reward_scored === null ? "—" : Number(c.mean_reward_scored).toFixed(3)),
      cell(tokens(c, "input_tokens")), cell(tokens(c, "output_tokens")))));
}

function evidenceLinks(job, trial) {
  const links = trial.evidence_links && trial.evidence_links.length ? trial.evidence_links
    : trial.evidence ? [{ label: "result.json", path: trial.evidence }] : [];
  if (!links.length) return h("span", { class: "muted", text: "—" });
  return h("div", { class: "row nowrap", style: "gap:4px 10px" }, links.map(item => h("a", {
    href: `/api/evidence/${encodeURIComponent(job.name)}/${item.path}`,
    target: "_blank", rel: "noopener", text: item.label,
  })));
}

function outcomeBadge(t) {
  return t.error ? badge(t.error_label || "异常", "bad")
    : t.reward === 1 ? badge("通过", "ok")
    : t.reward === null || t.reward === undefined ? badge("未评分", "warn")
    : badge("未通过", "bad");
}

function toolFailureText(trial) {
  const failures = (trial.diagnostics || {}).tool_failures;
  if (!failures) return null;
  return trial.tool_failure_label || `工具调用失败 ${failures.total} 次（仅已报告步骤）`;
}

function diagnosisCell(trial, onOpen) {
  const tools = toolFailureText(trial);
  const headline = trial.error || trial.error_label;
  return h("div", { style: "min-width:220px;max-width:420px" },
    headline ? h("div", {}, h("strong", { text: headline })) : null,
    trial.error && trial.error_label && trial.error_label !== trial.error ? h("div", { text: trial.error_label }) : null,
    trial.error_message ? h("div", { class: "clamp", text: trial.error_message, title: trial.error_message }) : null,
    trial.error_hint ? h("div", { class: "muted clamp", text: `处置：${trial.error_hint}`, title: trial.error_hint }) : null,
    trial.native_label ? h("div", { text: trial.native_label }) : null,
    tools ? h("div", { text: tools }) : null,
    h("button", { class: "link sm", style: "padding:0;margin-top:2px", text: "查看诊断详情", onclick: onOpen }));
}

function openTrialDrawer(job, t) {
  const diagnostics = t.diagnostics || {};
  const failures = diagnostics.tool_failures;
  const section = (title, ...body) => h("div", { class: "drawer-section" }, h("div", { class: "section-title", text: title }), body);
  const kv = (pairs) => h("dl", { class: "kv" }, pairs.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]));
  openDrawer({
    title: `Trial · ${t.trial}`,
    width: 720,
    content: [
      section("基本信息", kv([
        ["任务", t.task || "—"], ["Trial", t.trial], ["Harness", t.agent], ["Model", t.model], ["Dataset", t.dataset],
        ["结果", outcomeBadge(t)],
        ["Reward", t.reward === null || t.reward === undefined ? "—" : String(t.reward)],
        ["耗时", t.duration_sec === null || t.duration_sec === undefined ? "—" : `${t.duration_sec}s`],
        ["Token 入/出", `${t.input_tokens === null ? "未知" : num(t.input_tokens)} / ${t.output_tokens === null ? "未知" : num(t.output_tokens)}`],
      ])),
      section("证据", evidenceLinks(job, t)),
      t.error || t.error_label || t.error_message ? section("错误",
        alertBox(h("div", {},
          t.error_label && t.error_label !== t.error ? h("div", { text: t.error_label }) : null,
          t.error_message ? h("pre", { style: "margin:6px 0 0;background:transparent;border:0;padding:0", text: t.error_message }) : null,
          t.error_hint ? h("div", { class: "muted", style: "margin-top:6px", text: `处置：${t.error_hint}` }) : null),
        "error", t.error || t.error_label || "异常")) : null,
      section("诊断（不影响评分）",
        t.native_label ? h("div", { text: t.native_label }) : null,
        t.native_hint ? h("div", { class: "muted", text: t.native_hint }) : null,
        failures ? h("div", { text: toolFailureText(t) })
          : h("div", { class: "muted", text: "未提供结构化工具失败统计（不是 0 次）" }),
        failures && failures.reported_steps < failures.total_steps
          ? h("div", { class: "muted", text: `仅 ${failures.reported_steps}/${failures.total_steps} 步有工具统计，不能跨 harness 比较次数` }) : null,
        t.tool_failure_hint ? h("div", { class: "muted", text: t.tool_failure_hint }) : null,
        t.usage_hint ? h("div", { class: "muted", text: t.usage_hint }) : null),
      section("参数与原生证据",
        h("p", { class: "muted small", style: "margin:0 0 6px", text: "配置来自 result.json；未声明的值沿用原生默认，不代表已核验一致。原生 binding 是适配器配置证据，不是服务端确认。" }),
        h("pre", { text: JSON.stringify(diagnostics.configured || {}, null, 2) }),
        (diagnostics.steps || []).map(step => h("div", {},
          h("strong", { class: "small", text: `步骤 ${step.step}` }),
          h("pre", { text: JSON.stringify(step, null, 2) })))),
    ],
  });
}

function trialsCard(job) {
  const filter = { agent: "", model: "", dataset: "", outcome: "", q: "", limit: 100, offset: 0 };
  const results = h("div", {});
  const pager = h("div", { class: "pager" });
  const total = h("span", { class: "card-sub" });
  const selects = {};
  const OUTCOMES = [["passed", "通过"], ["failed", "未通过"], ["error", "异常"], ["unscored", "未评分（无异常）"]];
  const select = (label, key, options) => {
    const node = h("select", { "aria-label": label, onchange: (event) => { filter[key] = event.target.value; filter.offset = 0; load(); } },
      h("option", { value: "", text: label }));
    for (const value of options) {
      const [optionValue, optionLabel] = Array.isArray(value) ? value : [value, value];
      node.append(h("option", { value: optionValue, text: optionLabel }));
    }
    selects[key] = node;
    return node;
  };
  let searchTimer = null;
  const search = h("input", { class: "search", type: "search", placeholder: "搜索 trial / 任务 / 错误",
    oninput: (event) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { filter.q = event.target.value.trim(); filter.offset = 0; load(); }, 300);
    } });
  let seq = 0;

  async function load() {
    const mine = ++seq;
    const params = new URLSearchParams({ limit: filter.limit, offset: filter.offset });
    for (const key of ["agent", "model", "dataset", "outcome", "q"]) if (filter[key]) params.set(key, filter[key]);
    results.replaceChildren(spinner());
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(job.name)}/trials?${params}`);
      if (mine !== seq) return;
      const rows = data.trials.map(t => h("tr", {},
        h("td", { class: "nowrap", text: t.task || "—" }),
        h("td", { class: "mono nowrap", text: t.trial }),
        h("td", { class: "nowrap", text: t.agent }), h("td", { class: "nowrap", text: t.model }),
        h("td", { class: "nowrap", text: t.dataset }),
        h("td", {}, outcomeBadge(t)),
        h("td", { class: "num", text: t.reward === null || t.reward === undefined ? "—" : String(t.reward) }),
        h("td", { class: "num", text: t.duration_sec === null || t.duration_sec === undefined ? "—" : `${t.duration_sec}s` }),
        h("td", { class: "num", text: `${t.input_tokens === null ? "未知" : num(t.input_tokens)} / ${t.output_tokens === null ? "未知" : num(t.output_tokens)}` }),
        h("td", {}, evidenceLinks(job, t)),
        h("td", {}, diagnosisCell(t, () => openTrialDrawer(job, t)))));
      results.replaceChildren(table(["任务", "Trial", "Harness", "Model", "Dataset", "结果", ["Reward", "num"],
        ["耗时", "num"], ["Token 入/出", "num"], "证据", "诊断（不影响评分）"], rows, "没有符合条件的 trial"));
      const start = data.total ? filter.offset + 1 : 0;
      const end = Math.min(filter.offset + filter.limit, data.total);
      total.textContent = `共 ${num(data.total)} 条`;
      pager.replaceChildren(
        h("span", { class: "muted small", text: `第 ${start}–${end} 条，共 ${num(data.total)} 条` }),
        h("button", { class: "sm", text: "上一页", disabled: filter.offset === 0,
          onclick: () => { filter.offset = Math.max(0, filter.offset - filter.limit); load(); } }),
        h("button", { class: "sm", text: "下一页", disabled: end >= data.total,
          onclick: () => { filter.offset += filter.limit; load(); } }));
    } catch (error) {
      if (mine === seq) results.replaceChildren(errorBox(error));
    }
  }

  const reset = h("button", { class: "link", text: "重置筛选", onclick: () => {
    search.value = ""; filter.q = ""; apply({});
  } });
  const node = h("section", { class: "card" },
    h("div", { class: "card-header" },
      h("div", { class: "card-heading" }, h("h2", { class: "card-title", text: "逐 trial 明细" }), total)),
    h("div", { class: "card-body" },
      h("div", { class: "toolbar" },
        select("全部 harness", "agent", agentsOf(job.combos)), select("全部 model", "model", modelsOf(job.combos)),
        select("全部数据集", "dataset", datasetsOf(job.combos)), select("全部结果", "outcome", OUTCOMES),
        search, reset),
      results, pager));
  const apply = (values) => {
    for (const key of ["agent", "model", "dataset", "outcome"]) {
      filter[key] = values[key] || "";
      if (selects[key]) selects[key].value = filter[key];
    }
    filter.offset = 0;
    load();
  };
  load();
  return { node, apply };
}

function missingCard(job) {
  const total = job.combos.reduce((sum, c) => sum + (c.missing || 0), 0);
  if (!total) return null;
  const body = h("div", {}, muted("缺失指作业结束后仍未生成结果的 trial；运行中和排队中的任务不计入缺失。"));
  const detail = h("div", { style: "display:none" });
  let loaded = false;
  let open = false;
  const toggle = h("button", { class: "sm", text: "展开逐题明细", onclick: async () => {
    open = !open;
    toggle.textContent = open ? "收起" : "展开逐题明细";
    detail.style.display = open ? "" : "none";
    if (!open || loaded) return;
    loaded = true;
    detail.replaceChildren(spinner());
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(job.name)}/missing`);
      if (!data.missing.length) {
        detail.replaceChildren(h("div", { class: "inline-empty", text: "没有可枚举的缺失明细（作业可能使用整库或 glob）。" }));
        return;
      }
      detail.replaceChildren(
        table(["Dataset", "任务", "Harness", "Model", ["缺失 attempt", "num"]], data.missing.map(m => h("tr", {},
          h("td", { text: m.dataset }), h("td", { text: m.task }), h("td", { text: m.agent }),
          h("td", { text: m.model }), h("td", { class: "num", text: m.missing })))),
        h("p", { class: "muted small", text: `显示 ${num(data.missing.length)} 行，缺失 attempt 合计 ${num(data.total)}。` }));
    } catch (error) {
      loaded = false;
      detail.replaceChildren(errorBox(error));
    }
  } });
  body.append(detail);
  return card({ title: "缺失明细", sub: `缺失 attempt 合计 ${num(total)}`, extra: toggle }, body);
}

async function renderJob(name) {
  const job = await api(`/api/jobs/${encodeURIComponent(name)}`);
  await loadCatalog();
  setCrumbs([["结果看板", "#/jobs"], job.name]);
  const trials = trialsCard(job);
  const pick = (filters) => {
    trials.apply(filters);
    trials.node.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const sections = datasetsOf(job.combos).map(dataset =>
    heatSection(dataset, job.combos.filter(c => c.dataset === dataset), pick));
  const counts = job.counts;

  fill(app,
    job.fixture ? fixtureBanner() : null,
    pageHeader({ title: [job.name, fixtureBadge(job.fixture), job.status === "running" ? statusBadge("running") : null],
      back: "#/jobs",
      extra: [
        linkButton(`#/jobs/${encodeURIComponent(name)}/tasks`, "逐题对比"),
        linkButton(`#/compare?jobs=${encodeURIComponent(name)}`, "与其他作业对比"),
        job.run ? linkButton(`#/runs/${encodeURIComponent(name)}`, "运行详情", "primary") : null] }),
    card({ title: "总览" },
      h("div", { class: "stats" },
        stat("通过率（计划）", pct(job.pass_rate_planned), "primary"),
        stat("记录 / 计划", `${num(job.recorded_trials)} / ${num(job.planned_trials)}`),
        stat("已评分", num(counts.scored)),
        stat("通过", num(counts.passed), "ok"),
        stat("未通过", num(counts.failed)),
        stat("异常", num(counts.errors), counts.errors ? "bad" : ""),
        stat("运行中", num(counts.running)),
        stat("排队中", num(counts.queued)),
        stat("未评分（无异常）", num(counts.unscored), counts.unscored ? "warn" : ""),
        stat("缺失", num(counts.missing), counts.missing ? "warn" : ""),
        stat("组合数", num(job.combos.length))),
      descriptions([
        ["统计口径", "已评分 = 通过 + 未通过；异常与未评分（无异常）分别统计；运行中、排队中不计入缺失。"],
        ["证据目录", h("code", { text: job.path })],
      ]),
      job.error ? h("div", { class: "mt" }, alertBox(job.error, "error", "索引错误")) : null),
    job.status === "running" ? card({ title: "实时进度", sub: "每 10 秒自动刷新" },
      progressSummary(job.progress || null),
      h("h3", { text: "运行中的 trial" }),
      activeTable(job.progress ? job.progress.active : []),
      h("p", { class: "muted small", text: "外部启动的作业只读展示；从控制台发起的运行可在运行详情中取消。" })) : null,
    ...sections,
    card({ title: "组合汇总", flush: true }, comboTable(job.combos)),
    missingCard(job),
    trials.node,
  );
  if (job.status === "running") later(`#/jobs/${encodeURIComponent(name)}`, () => renderJob(name), 10000);
}

async function renderTasks(name) {
  const [job, data] = await Promise.all([
    api(`/api/jobs/${encodeURIComponent(name)}`),
    api(`/api/jobs/${encodeURIComponent(name)}/tasks`),
  ]);
  setCrumbs([["结果看板", "#/jobs"], [job.name, `#/jobs/${encodeURIComponent(name)}`], "逐题对比"]);
  const datasets = [...new Set(data.tasks.map(t => t.dataset))].sort();
  const state = { dataset: datasets[0] || "", diffOnly: true };
  const body = h("tbody", {});
  const count = h("span", { class: "card-sub" });

  function summary(cell) {
    if (!cell || !cell.recorded) return { text: "—", cls: "mark-na" };
    const parts = [];
    if (cell.passed) parts.push(`✓${cell.passed}`);
    if (cell.failed) parts.push(`✗${cell.failed}`);
    if (cell.error) parts.push(`E${cell.error}`);
    if (cell.unscored) parts.push(`?${cell.unscored}`);
    const cls = cell.error ? "mark-err" : cell.failed ? "mark-fail" : cell.passed ? "mark-pass" : "mark-na";
    return { text: parts.join(" "), cls };
  }

  function draw() {
    const rows = data.tasks.filter(t => !state.dataset || t.dataset === state.dataset)
      .filter(t => !state.diffOnly || new Set(t.cells.map(c => (c && c.passed) ? "p" : (c && c.recorded) ? "f" : "n")).size > 1);
    count.textContent = `${num(rows.length)} 道题`;
    body.replaceChildren(...(rows.length ? rows.map(task => h("tr", {},
      h("td", { class: "nowrap", text: task.task }),
      ...task.cells.map(cell => {
        const s = summary(cell);
        return h("td", { class: `num ${s.cls}`, title: cell && cell.recorded ? `通过 ${cell.passed} / 记录 ${cell.recorded}` : "无记录", text: s.text });
      }))) : [emptyRow(data.combos.length + 1, "没有符合条件的题目")]));
  }

  const select = h("select", { "aria-label": "数据集", onchange: (event) => { state.dataset = event.target.value; draw(); } },
    datasets.map(d => h("option", { value: d, text: d })));
  const toggle = h("label", { class: "check" },
    h("input", { type: "checkbox", checked: true, onchange: (event) => { state.diffOnly = event.target.checked; draw(); } }),
    "只看组合间有差异的题");

  draw();
  fill(app,
    job.fixture ? fixtureBanner() : null,
    pageHeader({ title: [job.name, h("span", { class: "muted", style: "font-weight:400", text: "· 逐题对比" })],
      back: `#/jobs/${encodeURIComponent(name)}` }),
    h("section", { class: "card" },
      h("div", { class: "card-header" },
        h("div", { class: "card-heading" }, h("h2", { class: "card-title", text: "逐题结果矩阵" }), count),
        h("div", { class: "card-extra" }, h("span", { class: "muted small" },
          h("span", { class: "mark-pass", text: "✓" }), " 通过　", h("span", { class: "mark-fail", text: "✗" }), " 失败　",
          h("span", { class: "mark-err", text: "E" }), " 错误　? 未评分；数字为 trial 数"))),
      h("div", { class: "card-body" },
        h("div", { class: "toolbar" }, select, toggle),
        h("div", { class: "table-wrap" },
          h("table", {},
            h("thead", {}, h("tr", {}, h("th", { text: "任务" }),
              data.combos.map(c => h("th", { class: "num" }, h("div", { text: c.agent }),
                h("div", { class: "muted", style: "font-weight:400", text: c.model }))))),
            body)))),
  );
}

async function renderCompare() {
  const data = await api("/api/jobs");
  setCrumbs(["评测", "作业对比"]);
  const MAX = 6;
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const initial = (params.get("jobs") || "").split(",").filter(Boolean).slice(0, MAX);
  const chosen = new Set(initial.length ? initial : data.jobs.slice(0, 4).map(j => j.name));
  const result = h("div", {});
  const counter = h("span", { class: "card-sub" });

  async function draw() {
    const names = [...chosen];
    counter.textContent = `已选 ${names.length} / ${MAX}`;
    if (!names.length) {
      result.replaceChildren(card({}, emptyState("至少选择一个作业。")));
      return;
    }
    result.replaceChildren(card({}, spinner()));
    try {
      const compared = await api(`/api/compare?jobs=${encodeURIComponent(names.join(","))}`);
      const rows = new Map();
      for (const entry of compared.jobs) {
        for (const combo of entry.combos) {
          const key = `${combo.dataset}\u0000${combo.agent}\u0000${combo.model}`;
          if (!rows.has(key)) rows.set(key, { dataset: combo.dataset, agent: combo.agent, model: combo.model, cells: {} });
          rows.get(key).cells[entry.job] = combo;
        }
      }
      result.replaceChildren(card({ title: "通过率对比", sub: "计划口径；括号内为 通过 / 计划", flush: true },
        table(["Dataset", "Harness", "Model", ...compared.jobs.map(j => [j.job, "num"])], [...rows.values()]
          .sort((a, b) => `${a.dataset}${a.agent}${a.model}`.localeCompare(`${b.dataset}${b.agent}${b.model}`))
          .map(row => h("tr", {},
            h("td", { class: "nowrap", text: row.dataset }), h("td", { class: "nowrap", text: row.agent }),
            h("td", { class: "nowrap", text: row.model }),
            ...compared.jobs.map(j => {
              const c = row.cells[j.job];
              if (!c) return h("td", { class: "num muted", text: "—" });
              return h("td", { class: "num", title: `通过 ${c.passed} / 计划 ${c.planned ?? "—"}，错误 ${c.errors}` },
                h("strong", { text: pct(c.pass_rate_planned) }),
                h("span", { class: "muted", text: ` (${c.passed}/${c.planned ?? "?"})` }));
            }))), "所选作业没有可比较的组合")));
    } catch (error) { result.replaceChildren(errorBox(error)); }
  }

  const chips = h("div", { class: "chips" }, data.jobs.map(job => {
    const chip = h("label", { class: `chip ${chosen.has(job.name) ? "on" : ""}`.trim() });
    const box = h("input", { type: "checkbox", checked: chosen.has(job.name),
      onchange: (event) => {
        if (event.target.checked && chosen.size >= MAX) {
          event.target.checked = false;
          toast.warning(`最多同时对比 ${MAX} 个作业`);
          return;
        }
        if (event.target.checked) chosen.add(job.name); else chosen.delete(job.name);
        chip.classList.toggle("on", event.target.checked);
        draw();
      } });
    chip.append(box, job.name, job.fixture ? badge("演示", "fixture") : null);
    return chip;
  }));

  draw();
  fill(app,
    pageHeader({ title: "作业对比", sub: "按组合比较跨作业的通过率（计划口径），用于观察升级或配置变化。" }),
    h("section", { class: "card" },
      h("div", { class: "card-header" },
        h("div", { class: "card-heading" }, h("h2", { class: "card-title", text: "选择作业" }), counter)),
      h("div", { class: "card-body" }, data.jobs.length ? chips : h("div", { class: "inline-empty", text: "还没有作业。" }))),
    result);
}

// --------------------------------------------------------------------- runs
function cancelRunDialog(name, onDone) {
  return confirmDialog({
    title: `停止运行 ${name}？`,
    danger: true,
    okText: "停止运行",
    content: [
      h("p", { text: "会终止 Harbor 进程组，并清理该运行占用的容器、网络与卷。" }),
      h("p", { class: "muted", text: "已完成的 trial 结果会保留，可继续在结果看板查看。" }),
    ],
    onOk: async () => {
      await api(`/api/runs/${encodeURIComponent(name)}/cancel`, { method: "POST" });
      toast.success("已发送停止请求，正在清理运行资源");
      if (onDone) await onDone();
    },
  });
}

async function renderRuns() {
  const data = await api("/api/runs");
  await loadCatalog();
  setCrumbs(["执行", "运行管理"]);
  const activeCount = data.runs.filter(run => ACTIVE.has(run.status)).length;
  const rows = data.runs.map(run => h("tr", {},
    h("td", {}, link(`#/runs/${encodeURIComponent(run.name)}`, run.name)),
    h("td", {}, statusBadge(run.status)),
    h("td", {}, run.dry_run ? badge("演练") : badge("真实", "warn")),
    h("td", {}, run.progress ? progressBar(run.progress) : h("span", { class: "muted", text: "—" })),
    h("td", { class: "num", text: run.progress
      ? `${num(run.progress.recorded)} / ${num(run.progress.planned)}（运行 ${num(run.progress.running)}）`
      : num(run.planned_trials) }),
    h("td", { class: "nowrap", text: clock(run.created_at) }),
    h("td", { class: "nowrap", text: run.finished_at ? clock(run.finished_at) : "—" }),
    h("td", {}, run.error ? h("div", { class: "clamp", style: "color:var(--bad)", title: run.error, text: run.error }) : ""),
    h("td", { class: "actions" }, h("div", { class: "row", style: "justify-content:flex-end" },
      link(`#/runs/${encodeURIComponent(run.name)}`, "详情"),
      ACTIVE.has(run.status) ? h("button", { class: "link danger", text: "停止",
        onclick: () => cancelRunDialog(run.name, renderRuns) }) : null))));
  fill(app,
    pageHeader({ title: "运行管理",
      sub: activeCount ? `${activeCount} 个运行进行中，列表每 5 秒自动刷新。` : "点击名称查看逐 trial 进度、日志与停止入口。",
      extra: [linkButton("#/jobs", "查看结果"), h("a", { class: "button primary", href: "#/runs/new" }, icon("plus"), "新建运行")] }),
    data.runs.length
      ? card({ title: "运行列表", sub: `共 ${data.runs.length} 条`, flush: true },
        table(["名称", "状态", "模式", "进度", ["Trial", "num"], "创建", "结束", "错误", ["操作", "num"]], rows))
      : card({}, emptyState("还没有运行记录。", linkButton("#/runs/new", "新建运行", "primary"))),
  );
  if (activeCount) later("#/runs", renderRuns, 5000);
}

async function renderRun(name) {
  setCrumbs([["运行管理", "#/runs"], name]);
  let offset = 0;
  let hasLog = false;
  const timer = { id: null };
  const statusSlot = h("span", {});
  const statsBox = h("div", { class: "stats" });
  const notice = h("div", {});
  const progressBox = h("div", { class: "muted", text: "等待计划生成…" });
  const activeBox = h("div", {});
  const log = h("pre", { class: "log" }, h("span", { class: "log-empty", text: "暂无日志输出…" }));
  const cancel = h("button", { class: "danger", style: "display:none",
    onclick: () => cancelRunDialog(name, refresh) }, icon("stop"), "停止运行");
  const jobLink = h("span", {});

  fill(app,
    pageHeader({ title: [name, statusSlot], back: "#/runs", extra: [jobLink, cancel] }),
    notice,
    card({ title: "运行信息" }, statsBox),
    card({ title: "实时进度", sub: "运行期间每 2 秒刷新" }, progressBox,
      h("h3", { text: "运行中的 trial" }), activeBox),
    card({ title: "运行日志", flush: true }, log));

  async function refresh() {
    try {
      const run = await api(`/api/runs/${encodeURIComponent(name)}`);
      statusSlot.replaceChildren(statusBadge(run.status));
      cancel.style.display = ACTIVE.has(run.status) ? "" : "none";
      statsBox.replaceChildren(
        stat("状态", (STATUS[run.status] || [run.status])[0], run.status === "failed" ? "bad" : run.status === "succeeded" ? "ok" : ""),
        stat("模式", run.dry_run ? "演练（无模型调用）" : "真实", run.dry_run ? "compact" : "warn compact"),
        stat("计划 trial", num(run.planned_trials)),
        stat("已记录", run.progress ? num(run.progress.recorded) : "—"),
        stat("运行中", run.progress ? num(run.progress.running) : "—"),
        stat("创建", clock(run.created_at), "compact"),
        stat("结束", run.finished_at ? clock(run.finished_at) : "—", "compact"));
      notice.replaceChildren(run.error ? alertBox(run.error, "error", "运行错误") : "");
      progressBox.replaceChildren(run.progress
        ? progressSummary(run.progress)
        : muted("job 目录尚未创建；计划渲染完成后开始统计。"));
      activeBox.replaceChildren(activeTable(run.progress ? run.progress.active : []));
      if (run.log_path) {
        const chunk = await api(`/api/runs/${encodeURIComponent(name)}/log?offset=${offset}`);
        if (chunk.text) {
          if (!hasLog) { log.textContent = ""; hasLog = true; }
          const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
          log.append(chunk.text);
          if (stick) log.scrollTop = log.scrollHeight;
        }
        offset = chunk.offset;
      }
      if (!ACTIVE.has(run.status)) {
        if (run.status === "succeeded" && !run.dry_run) {
          jobLink.replaceChildren(linkButton(`#/jobs/${encodeURIComponent(name)}`, "打开结果看板", "primary"));
          notice.replaceChildren(alertBox("结果已入库，可在结果看板查看通过率与逐题证据。", "success", "运行完成"));
        }
        clearInterval(timer.id);
        return false;
      }
      return true;
    } catch (error) {
      notice.replaceChildren(errorBox(error));
      return false;
    }
  }

  const active = await refresh();
  if (active) timer.id = setInterval(refresh, 2000);
  window.addEventListener("hashchange", () => clearInterval(timer.id), { once: true });
}

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

async function renderNewRun() {
  const catalogData = await loadCatalog(true);
  setCrumbs([["运行管理", "#/runs"], "新建运行"]);
  let matrixInfo = { yaml: "# 输入矩阵 YAML，字段与 configs/matrices/*.yaml 一致\n", fields: {} };
  try { matrixInfo = await api("/api/matrices/bailian-18"); } catch { /* keep fallback */ }
  const fields = matrixInfo.fields || {};
  const options = {
    benchmarks: [...new Set([...(catalogData.benchmarks || []), ...(fields.benchmarks || [])])],
    harnesses: [...new Set([...(catalogData.harnesses || []), ...(fields.harnesses || [])])],
    models: [...new Set([...(catalogData.models || []).map(m => m.profile), ...(fields.models || [])])],
  };
  const selected = {
    benchmarks: new Set(fields.benchmarks || options.benchmarks),
    harnesses: new Set(fields.harnesses || options.harnesses),
    models: new Set(fields.models || options.models),
  };
  let lastPreview = null;

  const matrix = h("textarea", { spellcheck: "false", text: matrixInfo.yaml, "aria-label": "矩阵 YAML" });
  const name = h("input", { value: `console-${stamp()}` });
  const concurrency = h("input", { type: "number", min: 1, placeholder: "矩阵默认" });
  const attempts = h("input", { type: "number", min: 1, placeholder: "矩阵默认" });
  const nTasks = h("input", { type: "number", min: 1, placeholder: "全量" });
  const tasks = h("input", { placeholder: "逗号分隔的题目目录名，可空" });
  const dryRun = h("input", { type: "checkbox", checked: !catalogData.allow_real_runs });
  if (!catalogData.allow_real_runs) dryRun.disabled = true;
  const gateNote = h("div", { class: "desc" });
  const modeHint = h("span", { class: "muted small" });
  const updateModeHint = () => {
    modeHint.textContent = dryRun.checked ? "演练：只验证计划渲染和调度，不调用模型。" : "真实运行：会调用付费模型，启动前需要二次确认。";
  };
  dryRun.addEventListener("change", updateModeHint);
  const setGate = (allowed) => {
    dryRun.disabled = !allowed;
    dryRun.checked = !allowed;
    gateNote.textContent = allowed
      ? "当前允许真实运行。默认真实执行；勾选演练模式可只验证计划而不调用模型。"
      : "当前仅演练模式：可以完整验证计划渲染和调度，但不会调用模型。";
    updateModeHint();
  };
  setGate(catalogData.allow_real_runs);

  const applyGate = async (next) => {
    const data = await api("/api/settings/real-runs", { method: "PUT", body: JSON.stringify({ allow: next }) });
    catalogData.allow_real_runs = data.allow_real_runs;
    if (catalog) catalog.allow_real_runs = data.allow_real_runs;
    setMode(data.allow_real_runs);
    setGate(data.allow_real_runs);
    allow.setChecked(data.allow_real_runs);
    toast.success(data.allow_real_runs ? "已允许真实运行" : "已切换为仅演练模式");
  };
  const allow = switchControl({ checked: catalogData.allow_real_runs, label: "允许真实运行",
    onChange: async (next) => {
      if (next) {
        await confirmDialog({
          title: "允许真实运行？",
          danger: true,
          okText: "允许真实运行",
          content: [
            h("p", { text: "开启后，新建运行默认真实执行并调用付费模型。" }),
            h("p", { class: "muted", text: "该设置保存在服务端，对所有访问此控制台的用户生效。" }),
          ],
          onOk: () => applyGate(true),
        });
        return false;
      }
      try { await applyGate(false); } catch (error) { toast.error(error.message); }
      return false;
    } });

  const preview = h("div", {});
  const message = h("div", {});
  preview.replaceChildren(h("div", { class: "inline-empty", text: "尚未预览。预览只渲染计划，不调用模型。" }));

  function overrides() {
    const value = { cluster: "docker" };
    if (concurrency.value) value.concurrency = Number(concurrency.value);
    if (attempts.value) value.attempts = Number(attempts.value);
    if (nTasks.value) value.n_tasks = Number(nTasks.value);
    if (tasks.value.trim()) value.tasks = tasks.value;
    for (const key of ["benchmarks", "harnesses", "models"]) {
      if (!selected[key].size) return null;
      value[key] = [...selected[key]];
    }
    return value;
  }

  function incompatibleCombos() {
    const incompatible = [];
    for (const model of selected.models) {
      const profile = (catalogData.models || []).find(m => m.profile === model);
      for (const harness of selected.harnesses) {
        const format = profile?.api_formats?.[harness];
        const supported = catalogData.harness_api_formats?.[harness];
        if (supported && !supported.includes(format)) incompatible.push(`${harness} × ${model}`);
      }
    }
    return incompatible;
  }
  const incompatibleText = (items) =>
    `模型协议不兼容：${items.join("、")}。矩阵会执行所有组合；Claude Code 请搭配 anthropic-compatible，或拆成独立矩阵。`;

  const counters = {};
  const boxes = {};
  function updateCounts() {
    for (const key of Object.keys(counters)) counters[key].textContent = `${selected[key].size} / ${options[key].length}`;
  }

  let syncTimer = null;
  let syncSeq = 0;
  async function sync() {
    const body = overrides();
    const mine = ++syncSeq;
    lastPreview = null;
    message.replaceChildren();
    if (!body) {
      preview.replaceChildren(alertBox("至少选择一个数据集、harness 和模型", "warning"));
      return;
    }
    const incompatible = incompatibleCombos();
    if (incompatible.length) {
      preview.replaceChildren(alertBox(incompatibleText(incompatible), "warning", "组合不兼容"));
      return;
    }
    preview.replaceChildren(spinner("渲染计划中…"));
    try {
      const data = await api("/api/runs/preview", { method: "POST",
        body: JSON.stringify({ matrix: matrix.value, overrides: body }) });
      if (mine !== syncSeq) return;
      matrix.value = data.matrix;
      lastPreview = data;
      preview.replaceChildren(h("div", { class: "stats" },
        stat("计划 trial", num(data.planned_trials), "primary"),
        stat("组合", num((data.combinations || []).length)),
        stat("执行后端", data.backend || "docker", "compact"),
        stat("范围", `${selected.benchmarks.size} 数据集 × ${selected.harnesses.size} harness × ${selected.models.size} 模型`, "compact")));
    } catch (error) { if (mine === syncSeq) preview.replaceChildren(errorBox(error)); }
  }
  function scheduleSync() { clearTimeout(syncTimer); syncTimer = setTimeout(sync, 300); }

  const pickGroup = (title, key) => {
    counters[key] = h("span", { class: "counter" });
    boxes[key] = [];
    const setAll = (on) => {
      selected[key] = new Set(on ? options[key] : []);
      for (const box of boxes[key]) box.checked = on;
      updateCounts();
      scheduleSync();
    };
    return h("div", { class: "checkgroup" },
      h("div", { class: "checkgroup-head" },
        h("div", { class: "row" }, h("strong", { text: title }), counters[key]),
        h("div", { class: "row", style: "gap:2px" },
          h("button", { class: "link sm", text: "全选", onclick: () => setAll(true) }),
          h("button", { class: "link sm", text: "清空", onclick: () => setAll(false) }))),
      h("div", { class: "checks" }, options[key].length ? options[key].map(option => {
        const box = h("input", { type: "checkbox", checked: selected[key].has(option),
          onchange: (event) => {
            if (event.target.checked) selected[key].add(option); else selected[key].delete(option);
            updateCounts();
            scheduleSync();
          } });
        boxes[key].push(box);
        return h("label", { class: "check" }, box, option);
      }) : h("div", { class: "inline-empty", style: "padding:8px", text: "无可选项" })));
  };

  for (const node of [concurrency, attempts, nTasks, tasks]) node.addEventListener("input", scheduleSync);

  const previewButton = h("button", { text: "预览计划", onclick: sync });

  async function submit(runName, body) {
    await api("/api/runs", { method: "POST", body: JSON.stringify({
      matrix: matrix.value, name: runName, dry_run: dryRun.checked, overrides: body }) });
    toast.success(`运行 ${runName} 已提交`);
    location.hash = `#/runs/${encodeURIComponent(runName)}`;
  }

  const startButton = h("button", { class: "primary", text: "启动运行", onclick: async () => {
    message.replaceChildren();
    const body = overrides();
    const runName = name.value.trim();
    if (!body) { toast.warning("至少选择一个数据集、harness 和模型"); return; }
    if (!runName) { toast.warning("请填写运行名称"); name.focus(); return; }
    if (!dryRun.checked) {
      const incompatible = incompatibleCombos();
      await confirmDialog({
        title: "确认启动真实运行？",
        danger: true,
        okText: "确认启动",
        content: [
          h("p", { text: "这是真实运行，会调用付费模型。" }),
          h("dl", { class: "kv" },
            h("dt", { text: "运行名称" }), h("dd", { text: runName }),
            h("dt", { text: "计划 trial" }), h("dd", { text: lastPreview ? num(lastPreview.planned_trials) : "未预览" }),
            h("dt", { text: "组合" }), h("dd", { text: lastPreview ? num((lastPreview.combinations || []).length) : "—" }),
            h("dt", { text: "范围" }), h("dd", { text: `${selected.benchmarks.size} 数据集 × ${selected.harnesses.size} harness × ${selected.models.size} 模型` }),
            h("dt", { text: "并发" }), h("dd", { text: concurrency.value || "矩阵默认" })),
          incompatible.length ? alertBox(incompatibleText(incompatible), "warning") : null,
        ],
        onOk: () => submit(runName, body),
      });
      return;
    }
    setLoading(startButton, true, "提交中…");
    try { await submit(runName, body); }
    catch (error) { message.replaceChildren(alertBox(error.message || String(error), "error", "启动失败")); setLoading(startButton, false); }
  } });

  const field = (label, input, help) => h("label", { class: "field" },
    h("span", { class: "label", text: label }), input, help ? h("span", { class: "help", text: help }) : null);

  fill(app,
    pageHeader({ title: "新建运行", back: "#/runs", sub: "配置运行参数与范围，预览计划后启动；所有修改实时同步到矩阵 YAML。" }),
    card({ title: "运行权限" },
      h("div", { class: "setting-row" },
        h("div", {}, h("div", { class: "title", text: "允许真实运行（调用付费模型）" }), gateNote),
        allow)),
    h("section", { class: "card" },
      h("div", { class: "card-header" }, h("div", { class: "card-heading" }, h("h2", { class: "card-title", text: "运行配置" }))),
      h("div", { class: "card-body" },
        h("div", { class: "form-section" },
          h("div", { class: "section-title", text: "基本参数" }),
          h("div", { class: "grid-form" },
            field("运行名称", name, "小写字母、数字、点、下划线和连字符"),
            field("并发", concurrency),
            field("Attempts", attempts),
            field("每数据集题数", nTasks),
            field("指定题目", tasks)),
          h("div", { class: "row mt small" }, h("span", { class: "muted", text: "执行后端" }), badge("docker", "ok"),
            h("span", { class: "muted", text: "当前 ECS 本机 Docker；不再提供后端选择。" }))),
        h("div", { class: "form-section" },
          h("div", { class: "section-title", text: "实际运行范围" }),
          h("div", { class: "checkgrid" },
            pickGroup("数据集", "benchmarks"), pickGroup("Harness", "harnesses"), pickGroup("模型", "models"))),
        h("div", { class: "form-section" },
          h("div", { class: "section-title", text: "计划预览" }),
          preview,
          message)),
      h("div", { class: "action-bar" },
        h("label", { class: "check" }, dryRun, "演练模式（--dry-run，不调用模型）"),
        h("div", { class: "grow" }, modeHint),
        previewButton, startButton)),
    h("details", { class: "card" },
      h("summary", { text: "高级：矩阵 YAML（同步后的实际生效配置）" }),
      h("div", { class: "details-body" },
        h("p", { class: "muted small", style: "margin-top:0", text: "修改任意字段或勾选后，系统会把 overrides 写入这段 YAML；启动时直接提交该 YAML，确保所见即所得。" }),
        matrix)),
    card({ title: "模型凭证状态", sub: "凭证只从运行控制台的环境变量读取，控制台不保存也不显示密钥值。", flush: true },
      table(["模型 profile", "环境变量", "状态"], catalogData.models.map(m => h("tr", {},
        h("td", { text: m.profile }), h("td", {}, m.api_key_name ? h("code", { text: m.api_key_name }) : "—"),
        h("td", {}, m.configured ? badge("已设置", "ok") : badge("未设置", "warn")))), "没有模型配置")),
  );
  updateCounts();
  scheduleSync();
}

window.addEventListener("hashchange", route);
route();
