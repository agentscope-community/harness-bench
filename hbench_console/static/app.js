const app = document.getElementById("app");
const modeEl = document.getElementById("mode");
const ACTIVE = new Set(["queued", "planning", "running", "summarizing", "archiving"]);
const STATUS = {
  queued: ["排队", "info"], planning: ["生成计划", "info"], running: ["运行中", "info"],
  summarizing: ["汇总中", "info"], archiving: ["归档中", "info"],
  succeeded: ["成功", "ok"], failed: ["失败", "bad"], cancelled: ["已取消", "warn"],
};
let catalog = null;

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) throw new Error((data && data.detail) || `${response.status} ${response.statusText}`);
  return data;
}

const pct = v => (v === null || v === undefined) ? "—" : `${(v * 100).toFixed(1)}%`;
const num = v => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("zh-CN");
const clock = s => s ? new Date(s).toLocaleString("zh-CN", { hour12: false }) : "—";
const heatColor = p => p === null || p === undefined ? null : `hsl(${Math.round(120 * Math.max(0, Math.min(1, p)))} 62% 40%)`;

function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity)
    .filter(child => child !== null && child !== undefined && child !== false));
}

function badge(text, kind = "") { return h("span", { class: `badge ${kind}`.trim(), text }); }
function statusBadge(status) {
  const [label, kind] = STATUS[status] || [status, ""];
  return badge(label, kind);
}
function errorBox(error) { return h("p", { class: "error", text: error.message || String(error) }); }
function link(href, text) { return h("a", { href, text }); }

async function loadCatalog(force = false) {
  if (!catalog || force) catalog = await api("/api/catalog");
  modeEl.textContent = catalog.allow_real_runs ? "真实运行已授权" : "仅演练模式";
  modeEl.className = `mode ${catalog.allow_real_runs ? "live" : ""}`;
  return catalog;
}

// ---------------------------------------------------------------- navigation
function setNav(section) {
  document.querySelectorAll(".topbar nav a").forEach(a =>
    a.classList.toggle("active", a.dataset.nav === section));
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
  const hash = location.hash || "#/jobs";
  for (const [pattern, view, section] of views) {
    const match = hash.match(pattern);
    if (!match) continue;
    setNav(section);
    fill(app, h("p", { class: "muted", text: "加载中…" }));
    try { await view(match); } catch (error) { fill(app, errorBox(error)); }
    return;
  }
  location.hash = "#/jobs";
}

// ------------------------------------------------------------------- helpers
function stat(label, value) {
  return h("div", { class: "stat" }, h("div", { class: "k", text: label }), h("div", { class: "v", text: value }));
}
function fixtureBanner() {
  return h("div", { class: "banner" },
    "演示数据：该作业由 fixture 生成，仅用于验证界面，不代表任何真实评测结果。");
}
function fixtureBadge(value) { return value ? badge("演示数据", "fixture") : null; }

function datasetsOf(combos) { return [...new Set(combos.map(c => c.dataset))].sort(); }
function agentsOf(combos) { return [...new Set(combos.map(c => c.agent))].sort(); }
function modelsOf(combos) { return [...new Set(combos.map(c => c.model))].sort(); }

function heatSection(dataset, combos, onPick) {
  const agents = agentsOf(combos), models = modelsOf(combos);
  const grid = h("div", { class: "heat" });
  grid.style.gridTemplateColumns = `170px repeat(${models.length}, minmax(130px, 1fr))`;
  grid.append(h("div", { class: "head", text: "harness \\ model" }));
  for (const model of models) grid.append(h("div", { class: "head", text: model }));
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
      const node = h("div", { class: "cell", title: "点击查看未通过 trial" },
        h("span", { class: "p", text: pct(cell.pass_rate_planned) }),
        h("span", { class: "d", text: `通过 ${cell.passed}/${cell.planned ?? cell.recorded}` }),
        notes.length ? h("span", { class: "d", text: notes.join(" · ") }) : null);
      if (color) node.style.background = color;
      node.addEventListener("click", () => onPick({ agent, model, dataset, outcome: "failed" }));
      grid.append(node);
    }
  }
  return h("div", { class: "card" },
    h("div", { class: "spread" },
      h("h2", { text: dataset }),
      h("span", { class: "legend" },
        h("span", { class: "swatch", style: "background:hsl(0 62% 40%)" }), "0%",
        h("span", { class: "swatch", style: "background:hsl(60 62% 40%)" }), "50%",
        h("span", { class: "swatch", style: "background:hsl(120 62% 40%)" }), "100%")),
    grid);
}

// ------------------------------------------------------------------- results
async function renderJobs() {
  const data = await api("/api/jobs");
  const catalogData = await loadCatalog();
  const syncText = data.sync.last ? `上次同步 ${clock(data.sync.last)}` : "尚未同步";
  const sync = h("button", {
    text: "同步证据目录",
    onclick: async (event) => {
      event.target.disabled = true;
      event.target.textContent = "同步中…";
      try { await api("/api/jobs/sync", { method: "POST" }); location.reload(); }
      catch (error) { app.prepend(errorBox(error)); event.target.disabled = false; event.target.textContent = "同步证据目录"; }
    },
  });

  fill(app, 
    h("div", { class: "spread" },
      h("h1", { text: "评测结果" }),
      h("div", { class: "row" }, h("span", { class: "muted", text: syncText }), sync)),
    h("p", { class: "muted", text: `证据目录来自服务端配置；共 ${data.jobs.length} 个作业。` }),
    data.sync.error ? h("p", { class: "error", text: `同步错误：${data.sync.error}` }) : null,
    data.jobs.length ? h("div", { class: "table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {},
          h("th", { text: "作业" }), h("th", { text: "来源" }), h("th", { text: "组合" }),
          h("th", { class: "num", text: "记录 / 计划" }), h("th", { class: "num", text: "通过率（计划）" }),
          h("th", { class: "num", text: "错误" }), h("th", { text: "更新时间" }), h("th", { text: "备注" }))),
        h("tbody", {}, data.jobs.map(job => h("tr", {},
          h("td", {}, link(`#/jobs/${encodeURIComponent(job.name)}`, job.name), " ",
            fixtureBadge(job.fixture), job.error ? badge("索引错误", "bad") : null),
          h("td", {}, badge(job.origin === "run" ? "控制台" : "导入", job.origin === "run" ? "info" : "")),
          h("td", { class: "num", text: num(job.combos) }),
          h("td", { class: "num", text: `${num(job.recorded_trials)} / ${num(job.planned_trials)}` }),
          h("td", { class: "num", text: pct(job.pass_rate_planned) }),
          h("td", { class: "num", text: num(job.errors) }),
          h("td", { text: clock(job.updated_at) }),
          h("td", { class: "muted", text: job.error || "" }))))))
      : h("div", { class: "card empty", text: "还没有结果。先运行一次评测，或把 OSS 上的作业目录同步到本机证据目录。" }),
    catalogData.viewer_url ? h("p", { class: "muted" },
      "逐题轨迹服务：", h("a", { href: catalogData.viewer_url, target: "_blank", rel: "noopener", text: catalogData.viewer_url })) : null,
  );
}

function comboTable(combos) {
  const cell = v => h("td", { class: "num", text: v });
  const tokens = (combo, field) => {
    const missing = combo[field + "_missing"];
    if (combo[field] === null || combo[field] === undefined) return "未知";
    return `${num(combo[field])}${missing ? `（已记录小计，缺 ${missing} 题）` : ""}`;
  };
  return h("div", { class: "table-wrap" },
    h("table", {},
      h("thead", {}, h("tr", {},
        ["Harness", "Model", "Dataset"].map(x => h("th", { text: x })),
        ["计划", "记录", "缺失", "错误", "未评分", "通过", "通过率（计划）", "通过率（记录）",
         "平均 reward", "输入 token", "输出 token"].map(x => h("th", { class: "num", text: x })))),
      h("tbody", {}, combos.map(c => h("tr", {},
        h("td", { text: c.agent }), h("td", { text: c.model }), h("td", { text: c.dataset }),
        cell(c.planned), cell(c.recorded), cell(c.missing), cell(c.errors), cell(c.unscored),
        cell(c.passed), cell(pct(c.pass_rate_planned)), cell(pct(c.pass_rate_recorded)),
        cell(c.mean_reward_scored === null ? "—" : Number(c.mean_reward_scored).toFixed(3)),
        cell(tokens(c, "input_tokens")), cell(tokens(c, "output_tokens")))))));
}

function evidenceLinks(job, trial) {
  const links = trial.evidence_links && trial.evidence_links.length ? trial.evidence_links
    : trial.evidence ? [{ label: "result.json", path: trial.evidence }] : [];
  if (!links.length) return h("span", { class: "muted", text: "—" });
  const parts = [];
  links.forEach((item, index) => {
    if (index) parts.push(" · ");
    parts.push(h("a", {
      href: `/api/evidence/${encodeURIComponent(job.name)}/${item.path}`,
      target: "_blank", rel: "noopener", text: item.label,
    }));
  });
  return h("div", {}, parts);
}

function diagnosisCell(trial) {
  const diagnostics = trial.diagnostics || {};
  const failures = diagnostics.tool_failures;
  const details = h("details", {}, h("summary", { text: "参数与原生证据" }),
    h("p", { class: "muted", text: "配置来自 result.json；未声明的值沿用原生默认，不代表已核验一致。原生 binding 是适配器配置证据，不是服务端确认。" }),
    h("pre", { text: JSON.stringify(diagnostics.configured || {}, null, 2) }),
    (diagnostics.steps || []).map(step => h("div", {},
      h("strong", { text: `步骤 ${step.step}` }),
      h("pre", { text: JSON.stringify(step, null, 2) }))));
  return h("div", {},
    trial.error || trial.error_label ? h("strong", { text: trial.error || trial.error_label }) : null,
    trial.error && trial.error_label && trial.error_label !== trial.error
      ? h("div", { text: trial.error_label }) : null,
    trial.error_message ? h("div", { text: trial.error_message, title: trial.error_message }) : null,
    trial.error_hint ? h("div", { class: "muted", text: `处置：${trial.error_hint}` }) : null,
    trial.native_label ? h("div", { text: trial.native_label }) : null,
    trial.native_hint ? h("div", { class: "muted", text: trial.native_hint }) : null,
    failures ? h("div", { text: trial.tool_failure_label || `工具调用失败 ${failures.total} 次（仅已报告步骤）` })
      : h("div", { class: "muted", text: "未提供结构化工具失败统计（不是 0 次）" }),
    failures && failures.reported_steps < failures.total_steps
      ? h("div", { class: "muted", text: `仅 ${failures.reported_steps}/${failures.total_steps} 步有工具统计，不能跨 harness 比较次数` }) : null,
    trial.tool_failure_hint ? h("div", { class: "muted", text: trial.tool_failure_hint }) : null,
    trial.usage_hint ? h("div", { class: "muted", text: trial.usage_hint }) : null,
    details);
}

function trialsCard(job) {
  const filter = { agent: "", model: "", dataset: "", outcome: "", q: "", limit: 100, offset: 0 };
  const results = h("div", {});
  const pager = h("div", { class: "pager" });
  const selects = {};
  const OUTCOMES = [["passed", "通过"], ["failed", "失败"], ["error", "错误"], ["unscored", "未评分"]];
  const select = (label, key, options) => {
    const node = h("select", { onchange: (event) => { filter[key] = event.target.value; filter.offset = 0; load(); } },
      h("option", { value: "", text: label }));
    for (const value of options) {
      const [optionValue, optionLabel] = Array.isArray(value) ? value : [value, value];
      node.append(h("option", { value: optionValue, text: optionLabel }));
    }
    selects[key] = node;
    return node;
  };
  const agents = [...new Set(job.combos.map(c => c.agent))].sort();
  const models = [...new Set(job.combos.map(c => c.model))].sort();
  const datasets = [...new Set(job.combos.map(c => c.dataset))].sort();
  const search = h("input", { placeholder: "搜索 trial / 任务 / 错误", size: 24,
    oninput: (event) => { filter.q = event.target.value.trim(); filter.offset = 0; load(); } });

  async function load() {
    const params = new URLSearchParams({ limit: filter.limit, offset: filter.offset });
    for (const key of ["agent", "model", "dataset", "outcome", "q"]) if (filter[key]) params.set(key, filter[key]);
    results.replaceChildren(h("p", { class: "muted", text: "加载中…" }));
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(job.name)}/trials?${params}`);
      const rows = data.trials.map(t => {
        const outcome = t.error ? badge(t.error_label || "错误", "bad")
          : t.reward === 1 ? badge("通过", "ok")
          : t.reward === null || t.reward === undefined ? badge("未评分", "warn")
          : badge("失败", "bad");
        return h("tr", {},
          h("td", { text: t.task || "—" }),
          h("td", { text: t.trial }),
          h("td", { text: t.agent }), h("td", { text: t.model }), h("td", { text: t.dataset }),
          h("td", {}, outcome),
          h("td", { class: "num", text: t.reward === null || t.reward === undefined ? "—" : String(t.reward) }),
          h("td", { class: "num", text: t.duration_sec === null || t.duration_sec === undefined ? "—" : `${t.duration_sec}s` }),
          h("td", { class: "num", text: `${t.input_tokens === null ? "未知" : num(t.input_tokens)} / ${t.output_tokens === null ? "未知" : num(t.output_tokens)}` }),
          h("td", {}, evidenceLinks(job, t)),
          h("td", { class: "muted" }, diagnosisCell(t)));
      });
      results.replaceChildren(h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, ["任务", "Trial", "Harness", "Model", "Dataset", "结果", "Reward", "耗时", "Token 入/出", "证据", "诊断（不影响评分）"].map(x => h("th", { text: x })))),
          h("tbody", {}, rows))));
      const start = data.total ? filter.offset + 1 : 0;
      const end = Math.min(filter.offset + filter.limit, data.total);
      pager.replaceChildren(
        h("button", { text: "上一页", disabled: filter.offset === 0,
          onclick: () => { filter.offset = Math.max(0, filter.offset - filter.limit); load(); } }),
        h("span", { class: "muted", text: `${start}–${end} / ${data.total}` }),
        h("button", { text: "下一页", disabled: end >= data.total,
          onclick: () => { filter.offset += filter.limit; load(); } }));
    } catch (error) {
      results.replaceChildren(errorBox(error));
    }
  }

  const node = h("div", { class: "card" },
    h("h2", { text: "逐 trial 明细" }),
    h("div", { class: "toolbar" },
      select("全部 harness", "agent", agents), select("全部 model", "model", models),
      select("全部数据集", "dataset", datasets), select("全部结果", "outcome", OUTCOMES), search),
    results, pager);
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
  const body = h("div", {});
  const details = h("details", {},
    h("summary", { text: `未运行 trial：${num(total)}（展开逐题明细）` }),
    h("p", { class: "muted", text: "缺失 = job 计划 - 已记录 trial。用于定位调度取消、地址池耗尽或任务未启动。" }),
    body);
  let loaded = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    body.replaceChildren(h("p", { class: "muted", text: "加载中…" }));
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(job.name)}/missing`);
      if (!data.missing.length) {
        body.replaceChildren(h("p", { class: "muted", text: "没有可枚举的缺失明细（作业可能使用整库或 glob）。" }));
        return;
      }
      body.replaceChildren(
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Dataset", "任务", "Harness", "Model", "缺失 attempt"].map(x => h("th", { text: x })))),
          h("tbody", {}, data.missing.map(m => h("tr", {},
            h("td", { text: m.dataset }), h("td", { text: m.task }), h("td", { text: m.agent }),
            h("td", { text: m.model }), h("td", { class: "num", text: m.missing })))))),
        h("p", { class: "muted", text: `显示 ${num(data.missing.length)} 行，缺失 attempt 合计 ${num(data.total)}。` }));
    } catch (error) {
      body.replaceChildren(errorBox(error));
    }
  });
  return h("div", { class: "card" }, h("h2", { text: "缺失明细" }), details);
}

async function renderJob(name) {
  const job = await api(`/api/jobs/${encodeURIComponent(name)}`);
  await loadCatalog();
  const trials = trialsCard(job);
  const pick = (filters) => {
    trials.apply(filters);
    trials.node.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const sections = datasetsOf(job.combos).map(dataset =>
    heatSection(dataset, job.combos.filter(c => c.dataset === dataset), pick));
  const totalPlanned = job.combos.reduce((sum, c) => sum + (c.planned || 0), 0);
  const totalPassed = job.combos.reduce((sum, c) => sum + (c.passed || 0), 0);
  const totalErrors = job.combos.reduce((sum, c) => sum + (c.errors || 0), 0);
  const totalMissing = job.combos.reduce((sum, c) => sum + (c.missing || 0), 0);

  fill(app, 
    job.fixture ? fixtureBanner() : null,
    h("div", { class: "spread" },
      h("h1", {}, job.name, " ", fixtureBadge(job.fixture)),
      h("div", { class: "row" },
        link(`#/jobs/${encodeURIComponent(name)}/tasks`, "逐题对比"),
        link(`#/compare?jobs=${encodeURIComponent(name)}`, "与其他作业对比"),
        link("#/jobs", "返回列表"))),
    h("div", { class: "card" },
      h("div", { class: "stats" },
        stat("记录 / 计划", `${num(job.recorded_trials)} / ${num(job.planned_trials)}`),
        stat("通过 / 计划", `${num(totalPassed)} / ${num(totalPlanned)}`),
        stat("通过率（计划）", pct(job.pass_rate_planned)),
        stat("错误 trial", num(totalErrors)),
        stat("缺失 trial", num(totalMissing)),
        stat("组合数", num(job.combos.length))),
      h("p", { class: "muted", text: job.note || "" }),
      h("p", { class: "muted" }, "证据目录：", h("code", { text: job.path })),
      job.error ? h("p", { class: "error", text: `索引错误：${job.error}` }) : null),
    ...sections,
    h("div", { class: "card" }, h("h2", { text: "组合汇总" }), comboTable(job.combos)),
    missingCard(job),
    trials.node,
  );
}

async function renderTasks(name) {
  const [job, data] = await Promise.all([
    api(`/api/jobs/${encodeURIComponent(name)}`),
    api(`/api/jobs/${encodeURIComponent(name)}/tasks`),
  ]);
  const datasets = [...new Set(data.tasks.map(t => t.dataset))].sort();
  const state = { dataset: datasets[0] || "", diffOnly: true };
  const body = h("tbody", {});

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
    body.replaceChildren(...(rows.length ? rows.map(task => h("tr", {},
      h("td", { text: task.task }),
      ...task.cells.map(cell => {
        const s = summary(cell);
        return h("td", { class: `num ${s.cls}`, title: cell && cell.recorded ? `通过 ${cell.passed} / 记录 ${cell.recorded}` : "无记录", text: s.text });
      }))) : [h("tr", {}, h("td", { colspan: data.combos.length + 1, class: "muted", text: "没有符合条件的题目" }))]));
  }

  const select = h("select", { onchange: (event) => { state.dataset = event.target.value; draw(); } },
    datasets.map(d => h("option", { value: d, text: d })));
  const toggle = h("label", { class: "row" },
    h("input", { type: "checkbox", checked: true, onchange: (event) => { state.diffOnly = event.target.checked; draw(); } }),
    "只看组合间有差异的题");

  draw();
  fill(app, 
    job.fixture ? fixtureBanner() : null,
    h("div", { class: "spread" },
      h("h1", {}, job.name, " · 逐题对比"),
      h("div", { class: "row" }, link(`#/jobs/${encodeURIComponent(name)}`, "返回作业"), link("#/jobs", "结果列表"))),
    h("div", { class: "card" },
      h("div", { class: "toolbar" }, select, toggle,
        h("span", { class: "muted", text: "✓ 通过，✗ 失败，E 错误，? 未评分；数字为 trial 数。" })),
      h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, h("th", { text: "任务" }),
            data.combos.map(c => h("th", { class: "num" }, h("div", { text: c.agent }), h("div", { class: "muted", text: c.model }))))),
          body))),
  );
}

async function renderCompare() {
  const data = await api("/api/jobs");
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const initial = (params.get("jobs") || "").split(",").filter(Boolean).slice(0, 6);
  const chosen = new Set(initial.length ? initial : data.jobs.slice(0, 4).map(j => j.name));
  const result = h("div", {});

  async function draw() {
    const names = [...chosen];
    if (!names.length) { result.replaceChildren(h("p", { class: "muted", text: "至少选择一个作业。" })); return; }
    result.replaceChildren(h("p", { class: "muted", text: "加载中…" }));
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
      result.replaceChildren(h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, ["Dataset", "Harness", "Model", ...compared.jobs.map(j => j.job)].map(x => h("th", { text: x })))),
          h("tbody", {}, [...rows.values()]
            .sort((a, b) => `${a.dataset}${a.agent}${a.model}`.localeCompare(`${b.dataset}${b.agent}${b.model}`))
            .map(row => h("tr", {},
              h("td", { text: row.dataset }), h("td", { text: row.agent }), h("td", { text: row.model }),
              ...compared.jobs.map(j => {
                const c = row.cells[j.job];
                if (!c) return h("td", { class: "num muted", text: "—" });
                return h("td", { class: "num", title: `通过 ${c.passed} / 计划 ${c.planned ?? "—"}，错误 ${c.errors}`,
                  text: `${pct(c.pass_rate_planned)} (${c.passed}/${c.planned ?? "?"})` });
              })))))));
    } catch (error) { result.replaceChildren(errorBox(error)); }
  }

  const boxes = h("div", { class: "row" }, data.jobs.map(job => h("label", { class: "row" },
    h("input", { type: "checkbox", checked: chosen.has(job.name),
      onchange: (event) => { event.target.checked ? chosen.add(job.name) : chosen.delete(job.name); draw(); } }),
    job.name, job.fixture ? badge("演示", "fixture") : null)));

  draw();
  fill(app, 
    h("h1", { text: "作业对比" }),
    h("p", { class: "muted", text: "按组合比较跨作业的通过率（计划口径），用于观察升级或配置变化。" }),
    h("div", { class: "card" }, h("h3", { text: "选择作业（最多 6 个）" }), boxes),
    result);
}

// --------------------------------------------------------------------- runs
async function renderRuns() {
  const data = await api("/api/runs");
  const rows = data.runs.map(run => h("tr", {},
    h("td", {}, link(`#/runs/${encodeURIComponent(run.name)}`, run.name)),
    h("td", {}, statusBadge(run.status)),
    h("td", { text: run.dry_run ? "演练" : "真实" }),
    h("td", { class: "num", text: num(run.planned_trials) }),
    h("td", { text: clock(run.created_at) }),
    h("td", { text: run.finished_at ? clock(run.finished_at) : "—" }),
    h("td", { class: "muted", text: run.error || "" }),
    h("td", {}, ACTIVE.has(run.status) ? h("button", { class: "danger", text: "取消",
      onclick: async () => { try { await api(`/api/runs/${encodeURIComponent(run.name)}/cancel`, { method: "POST" }); route(); } catch (error) { alert(error.message); } } }) : "")));
  fill(app, 
    h("div", { class: "spread" }, h("h1", { text: "运行" }),
      h("div", { class: "row" }, h("a", { class: "button primary", href: "#/runs/new", text: "新建运行" }), link("#/jobs", "查看结果"))),
    data.runs.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["名称", "状态", "模式", "计划 trial", "创建", "结束", "错误", ""].map(x => h("th", { text: x })))),
      h("tbody", {}, rows)))
      : h("div", { class: "card empty", text: "还没有运行记录。" }),
  );
}

async function renderRun(name) {
  let offset = 0;
  const timer = { id: null };
  const statsBox = h("div", { class: "stats" });
  const log = h("pre", { class: "log", text: "" });
  const cancel = h("button", { class: "danger", text: "取消运行", style: "display:none",
    onclick: async () => {
      if (!confirm("取消会向 Harbor 进程组发送 SIGTERM，确定继续？")) return;
      try { await api(`/api/runs/${encodeURIComponent(name)}/cancel`, { method: "POST" }); }
      catch (error) { alert(error.message); }
    } });

  fill(app, 
    h("div", { class: "spread" },
      h("h1", {}, name, " ", h("span", { id: "run-status" })),
      h("div", { class: "row" }, link("#/runs", "返回运行列表"), cancel)),
    h("div", { class: "card" }, statsBox),
    h("div", { class: "card" }, h("h2", { text: "日志" }), log));

  async function refresh() {
    try {
      const run = await api(`/api/runs/${encodeURIComponent(name)}`);
      const status = document.getElementById("run-status");
      if (status) status.replaceWith(Object.assign(statusBadge(run.status), { id: "run-status" }));
      cancel.style.display = ACTIVE.has(run.status) ? "" : "none";
      statsBox.replaceChildren(
        stat("状态", (STATUS[run.status] || [run.status])[0]),
        stat("模式", run.dry_run ? "演练（无模型调用）" : "真实"),
        stat("计划 trial", num(run.planned_trials)),
        stat("创建", clock(run.created_at)),
        stat("结束", run.finished_at ? clock(run.finished_at) : "—"));
      if (run.error) statsBox.append(h("p", { class: "error", text: run.error }));
      if (run.log_path) {
        const chunk = await api(`/api/runs/${encodeURIComponent(name)}/log?offset=${offset}`);
        if (chunk.text) { log.textContent += chunk.text; log.scrollTop = log.scrollHeight; }
        offset = chunk.offset;
      }
      if (!ACTIVE.has(run.status)) {
        if (run.status === "succeeded" && !run.dry_run) {
          statsBox.append(h("p", {}, "结果已入库：", link(`#/jobs/${encodeURIComponent(name)}`, "打开看板")));
        }
        clearInterval(timer.id);
        return false;
      }
      return true;
    } catch (error) {
      statsBox.replaceChildren(errorBox(error));
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
  let defaultMatrix = "";
  try { defaultMatrix = (await api("/api/matrices/bailian-18")).yaml; }
  catch { defaultMatrix = "# 输入矩阵 YAML，字段与 configs/matrices/*.yaml 一致\n"; }

  const matrix = h("textarea", { spellcheck: "false", text: defaultMatrix });
  const name = h("input", { value: `console-${stamp()}`, size: 28 });
  const cluster = h("select", {}, h("option", { value: "", text: "矩阵默认" }),
    catalogData.clusters.map(c => h("option", { value: c, text: c })));
  const concurrency = h("input", { type: "number", min: 1, placeholder: "矩阵默认" });
  const attempts = h("input", { type: "number", min: 1, placeholder: "矩阵默认" });
  const nTasks = h("input", { type: "number", min: 1, placeholder: "全量" });
  const tasks = h("input", { placeholder: "逗号分隔的题目目录名，可空", size: 32 });
  const dryRun = h("input", { type: "checkbox", checked: true });
  if (!catalogData.allow_real_runs) dryRun.disabled = true;
  const preview = h("div", { class: "muted", text: "尚未预览。预览只渲染计划，不调用模型。" });
  const message = h("div", {});

  function overrides() {
    const value = {};
    if (cluster.value) value.cluster = cluster.value;
    if (concurrency.value) value.concurrency = Number(concurrency.value);
    if (attempts.value) value.attempts = Number(attempts.value);
    if (nTasks.value) value.n_tasks = Number(nTasks.value);
    if (tasks.value.trim()) value.tasks = tasks.value;
    return value;
  }

  const previewButton = h("button", { text: "预览计划", onclick: async () => {
    previewButton.disabled = true;
    preview.textContent = "渲染中…";
    preview.className = "muted";
    try {
      const data = await api("/api/runs/preview", { method: "POST",
        body: JSON.stringify({ matrix: matrix.value, overrides: overrides() }) });
      preview.replaceChildren(
        h("div", {}, "计划 trial：", h("strong", { text: num(data.planned_trials) }), "；组合：",
          h("strong", { text: num((data.combinations || []).length) }), "；后端：",
          h("strong", { text: data.backend || "—" }), "。" ),
        h("div", { class: "muted", text: "计划不执行；真实运行的耗时与费用取决于模型和并发。" }));
    } catch (error) { preview.replaceChildren(errorBox(error)); }
    finally { previewButton.disabled = false; }
  } });

  const startButton = h("button", { class: "primary", text: "启动", onclick: async () => {
    message.replaceChildren();
    if (!dryRun.checked && !confirm("这是真实运行，会调用付费模型。确定启动？")) return;
    startButton.disabled = true;
    try {
      await api("/api/runs", { method: "POST", body: JSON.stringify({
        matrix: matrix.value, name: name.value.trim(), dry_run: dryRun.checked, overrides: overrides() }) });
      location.hash = `#/runs/${encodeURIComponent(name.value.trim())}`;
    } catch (error) { message.replaceChildren(errorBox(error)); startButton.disabled = false; }
  } });

  fill(app, 
    h("div", { class: "spread" }, h("h1", { text: "新建运行" }), link("#/runs", "返回运行列表")),
    h("div", { class: "card" },
      h("p", { class: "muted", text: catalogData.allow_real_runs
        ? "当前允许真实运行。真实运行会调用百炼模型并产生费用。"
        : "当前仅演练模式：可以完整验证计划渲染和调度，但不会调用模型。管理员设置 CONSOLE_ALLOW_REAL_RUNS=1 后才能真实运行。" }),
      h("div", { class: "grid-form" },
        h("label", { class: "field" }, "运行名称", name),
        h("label", { class: "field" }, "执行后端", cluster),
        h("label", { class: "field" }, "并发", concurrency),
        h("label", { class: "field" }, "Attempts", attempts),
        h("label", { class: "field" }, "每数据集题数", nTasks),
        h("label", { class: "field" }, "指定题目", tasks)),
      h("label", { class: "row" }, dryRun, "演练模式（--dry-run，不调用模型）"),
      h("h3", { text: "矩阵 YAML" }),
      matrix,
      h("div", { class: "toolbar" }, previewButton, startButton),
      preview,
      message),
    h("div", { class: "card" },
      h("h2", { text: "模型凭证状态" }),
      h("p", { class: "muted", text: "凭证只从运行控制台的环境变量读取，控制台不保存也不显示密钥值。" }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["模型 profile", "环境变量", "状态"].map(x => h("th", { text: x })))),
        h("tbody", {}, catalogData.models.map(m => h("tr", {},
          h("td", { text: m.profile }), h("td", { text: m.api_key_name || "—" }),
          h("td", {}, m.configured ? badge("已设置", "ok") : badge("未设置", "warn")))))))),
  );
}

window.addEventListener("hashchange", route);
route();
