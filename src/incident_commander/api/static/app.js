// Incident Commander dashboard. No build step, no third-party code.
// Security: every string that came from the API (alert text, log lines, model output) is
// untrusted and is only ever inserted with textContent / createTextNode, never as HTML.
"use strict";

const state = {
  config: null,
  scenarios: [],
  current: null, // incident id
  tab: "timeline",
  cursor: 0,
  pollTimer: null,
  detail: null,
};

const $ = (id) => document.getElementById(id);
const TABS = ["timeline", "hypotheses", "remediation", "postmortem"];
const POLL_MS = 1200;

function el(tag, opts = {}, ...children) {
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined) node.textContent = String(opts.text);
  if (opts.title) node.title = opts.title;
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

// ------------------------------------------------------------------ API
function apiKey() {
  return sessionStorage.getItem("ic-api-key") || "";
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;
  const res = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const type = res.headers.get("content-type") || "";
  const payload = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    if (payload && typeof payload === "object" && payload.detail) {
      message = Array.isArray(payload.detail)
        ? payload.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
        : String(payload.detail);
    }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return payload;
}

// ------------------------------------------------------------- helpers
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toISOString().slice(11, 19);
}

function setStatusPill(node, value) {
  node.className = `pill st-${value || "open"}`;
  node.textContent = value || "queued";
}

function sevBadge(severity) {
  return el("span", { className: `sev ${severity || ""}`, text: severity || "…" });
}

// ------------------------------------------------------------- routing
function parseHash() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const tab = params.get("tab");
  return {
    incident: params.get("incident"),
    tab: TABS.includes(tab) ? tab : "timeline",
  };
}

function writeHash() {
  if (!state.current) return;
  const next = `#incident=${encodeURIComponent(state.current)}&tab=${state.tab}`;
  if (location.hash !== next) history.replaceState(null, "", next);
}

function applyHash() {
  const { incident, tab } = parseHash();
  showTab(tab, false);
  if (incident && /^[A-Za-z0-9_-]{1,64}$/.test(incident) && incident !== state.current) {
    selectIncident(incident);
  }
}

function showTab(tab, updateHash = true) {
  state.tab = tab;
  for (const btn of document.querySelectorAll(".tabs button")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
    btn.setAttribute("aria-selected", btn.dataset.tab === tab ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.hidden = panel.dataset.panel !== tab;
  }
  if (tab === "postmortem" && state.current) loadPostmortem();
  if (updateHash) writeHash();
}

// ------------------------------------------------------------ bootstrap
async function init() {
  for (const btn of document.querySelectorAll(".tabs button")) {
    btn.addEventListener("click", () => showTab(btn.dataset.tab));
  }
  $("start-form").addEventListener("submit", startIncident);
  $("refresh-btn").addEventListener("click", refreshList);
  $("scenario").addEventListener("change", describeScenario);
  $("api-key").addEventListener("change", (e) => {
    sessionStorage.setItem("ic-api-key", e.target.value.trim());
    loadEverything();
  });
  $("approver").value = localStorage.getItem("ic-approver") || "";
  $("approver").addEventListener("change", (e) =>
    localStorage.setItem("ic-approver", e.target.value.trim()),
  );
  $("copy-md").addEventListener("click", async () => {
    const text = $("postmortem").dataset.raw || "";
    try {
      await navigator.clipboard.writeText(text);
      $("copy-md").textContent = "Copied";
      setTimeout(() => ($("copy-md").textContent = "Copy Markdown"), 1500);
    } catch {
      $("copy-md").textContent = "Copy failed";
    }
  });
  window.addEventListener("hashchange", applyHash);
  await loadEverything();
  applyHash();
  setInterval(refreshList, 8000);
}

async function loadEverything() {
  try {
    state.config = await api("/api/config");
  } catch (err) {
    $("mode-badge").textContent = "API unreachable";
    return;
  }
  const cfg = state.config;
  $("version").textContent = `v${cfg.version}`;
  const labels = { "api-key": "API key", dev: "Dev mode", "public-demo": "Public demo" };
  $("mode-badge").textContent = labels[cfg.access_mode] || cfg.access_mode;
  $("mode-badge").title = cfg.simulated_note;
  $("key-row").hidden = !cfg.auth_required;
  $("api-key").value = apiKey();
  const provider = $("provider");
  provider.replaceChildren(
    ...cfg.providers.map((p) =>
      el("option", { text: p === "mock" ? "mock (offline, deterministic)" : p }),
    ),
  );
  [...provider.options].forEach((o, i) => (o.value = cfg.providers[i]));
  provider.value = cfg.default_provider;
  if (cfg.auth_required && !apiKey()) {
    $("start-error").textContent = "Enter an API key to use this server.";
    return;
  }
  try {
    const data = await api("/api/scenarios");
    state.scenarios = data.scenarios;
    const select = $("scenario");
    select.replaceChildren(
      ...state.scenarios.map((s) => {
        const option = el("option", { text: s.title });
        option.value = s.name;
        return option;
      }),
    );
    describeScenario();
    $("start-error").textContent = "";
  } catch (err) {
    $("start-error").textContent = err.message;
  }
  await refreshList();
}

function describeScenario() {
  const s = state.scenarios.find((x) => x.name === $("scenario").value);
  $("scenario-desc").textContent = s ? `${s.description} Services: ${s.services.join(", ")}.` : "";
}

// ------------------------------------------------------------- incidents
async function startIncident(event) {
  event.preventDefault();
  $("start-error").textContent = "";
  $("start-btn").disabled = true;
  try {
    const body = { scenario: $("scenario").value, provider: $("provider").value };
    const created = await api("/api/incidents", { method: "POST", body });
    await refreshList();
    await selectIncident(created.id, created);
  } catch (err) {
    $("start-error").textContent = err.message;
  } finally {
    $("start-btn").disabled = false;
  }
}

async function refreshList() {
  if (state.config && state.config.auth_required && !apiKey()) return;
  let data;
  try {
    data = await api("/api/incidents?limit=50");
  } catch {
    return;
  }
  const list = $("incident-list");
  list.replaceChildren(
    ...data.incidents.map((inc) => {
      const status = inc.state === "error" ? "error" : inc.status || inc.state;
      const li = el(
        "li",
        {},
        el(
          "div",
          { className: "row" },
          sevBadge(inc.severity),
          el("span", { className: "title", text: inc.title, title: inc.title }),
        ),
        el(
          "div",
          { className: "sub" },
          `${status} · ${inc.scenario} · ${new Date(inc.created_at).toLocaleTimeString()}`,
          inc.pending_approvals ? ` · ${inc.pending_approvals} awaiting approval` : "",
        ),
      );
      li.classList.toggle("active", inc.id === state.current);
      li.addEventListener("click", () => selectIncident(inc.id));
      return li;
    }),
  );
  $("list-empty").hidden = data.incidents.length > 0;
}

async function selectIncident(id, created = null) {
  stopPolling();
  state.current = id;
  state.cursor = 0;
  state.detail = null;
  $("timeline").replaceChildren();
  $("postmortem").replaceChildren();
  $("decision-error").textContent = "";
  $("empty-state").hidden = true;
  $("incident-view").hidden = false;
  writeHash();
  for (const li of $("incident-list").children) li.classList.remove("active");
  if (created && created.timeline && created.timeline.length) {
    // Sync (serverless) mode: the whole investigation came back with the POST.
    appendTimeline(created.timeline);
    state.cursor = created.timeline.length;
    renderDetail(created);
  }
  try {
    await refreshDetail();
  } catch (err) {
    $("inc-title").textContent = "Incident not found";
    $("inc-error").textContent = err.message;
    return;
  }
  showTab(state.tab, false);
  poll();
}

function stopPolling() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

async function poll() {
  const id = state.current;
  try {
    const data = await api(`/api/incidents/${encodeURIComponent(id)}/timeline?since=${state.cursor}`);
    if (id !== state.current) return;
    appendTimeline(data.entries);
    state.cursor = data.next_cursor;
    if (data.entries.length || !state.detail || state.detail.state !== data.state) {
      await refreshDetail();
    }
    if (data.done) {
      $("spinner").hidden = true;
      refreshList();
      return;
    }
  } catch (err) {
    $("inc-error").textContent = err.message;
  }
  state.pollTimer = setTimeout(poll, POLL_MS);
}

async function refreshDetail() {
  const id = state.current;
  const detail = await api(`/api/incidents/${encodeURIComponent(id)}`);
  if (id === state.current) renderDetail(detail);
}

function renderDetail(d) {
  state.detail = d;
  const head = $("inc-sev");
  head.className = `sev ${d.severity || ""}`;
  head.textContent = d.severity || "…";
  $("inc-title").textContent = d.title;
  setStatusPill($("inc-status"), d.state === "error" ? "error" : d.status);
  $("inc-state").textContent = `investigation: ${d.investigation_status || d.state}`;
  $("inc-id").textContent = d.id;
  $("inc-services").textContent = d.services.join(", ") || "–";
  $("inc-summary").textContent = d.summary || (d.state === "finished" ? "" : "Investigating…");
  $("inc-error").textContent = d.error || "";
  $("spinner").hidden = d.state === "finished" || d.state === "error";
  renderHypotheses(d);
  renderProposals(d);
  if (state.tab === "postmortem" && d.state === "finished") loadPostmortem();
}

function appendTimeline(entries) {
  const list = $("timeline");
  for (const e of entries) {
    list.append(
      el(
        "li",
        {},
        el("span", { className: "ts", text: fmtTime(e.ts) }),
        el(
          "span",
          {},
          el("span", { className: `kind ${e.kind}`, text: e.kind }),
          el("span", { className: "actor", text: e.actor }),
        ),
        el("span", { className: "text", text: e.text }),
      ),
    );
  }
}

function renderHypotheses(d) {
  const box = $("hypotheses");
  if (!d.hypotheses.length) {
    box.replaceChildren(el("p", { className: "muted", text: "No hypotheses yet." }));
    return;
  }
  const sorted = [...d.hypotheses].sort((a, b) => b.confidence - a.confidence);
  box.replaceChildren(
    ...sorted.map((h) => {
      const fill = el("span");
      fill.style.width = `${Math.round(h.confidence * 100)}%`;
      const evidence = el("ul", { className: "evidence" });
      for (const e of h.evidence_for) evidence.append(el("li", { className: "for", text: e }));
      for (const e of h.evidence_against) {
        evidence.append(el("li", { className: "against", text: e }));
      }
      const card = el(
        "div",
        { className: `hypothesis ${h.status}` },
        el(
          "div",
          { className: "head" },
          el("strong", { text: `${h.id} · ${h.suspected_cause.replaceAll("_", " ")}` }),
          el(
            "span",
            {},
            d.root_cause_id === h.id ? el("span", { className: "pill st-resolved", text: "root cause" }) : null,
            " ",
            el("span", { className: "pill", text: h.status }),
          ),
        ),
        el("div", { className: "muted small", text: h.service ? `service: ${h.service}` : "" }),
        el("div", { className: "bar" }, fill),
        el("div", { className: "small", text: `confidence ${(h.confidence * 100).toFixed(0)}%` }),
        el("p", { text: h.statement }),
        evidence,
      );
      if (d.root_cause_id === h.id) card.classList.add("root");
      return card;
    }),
  );
}

function renderProposals(d) {
  const box = $("proposals");
  const pending = d.proposals.filter((p) => p.status === "proposed").length;
  $("pending-count").hidden = pending === 0;
  $("pending-count").textContent = String(pending);
  if (!d.proposals.length) {
    const msg = d.state === "finished" ? "The agent did not propose a runbook." : "Waiting for the investigation…";
    box.replaceChildren(el("p", { className: "muted", text: msg }));
    return;
  }
  const canDecide = state.config && state.config.caller_role === "approver";
  box.replaceChildren(
    ...d.proposals.map((p) => {
      const params = Object.entries(p.params).map(([k, v]) => `${k}=${v}`).join(", ");
      const card = el(
        "div",
        { className: "proposal" },
        el(
          "div",
          { className: "head" },
          el("code", { text: `${p.runbook_id}(${params})` }),
          el("span", {}, el("span", { className: `risk-${p.risk}`, text: `risk ${p.risk}` }), " ", el("span", { className: "pill", text: p.status })),
        ),
        el("p", { text: p.rationale }),
        p.decided_by ? el("div", { className: "muted small", text: `decided by ${p.decided_by}` }) : null,
        p.result ? el("div", { className: "small", text: `result (simulated): ${p.result}` }) : null,
      );
      if (p.status === "proposed") {
        const reason = el("input");
        reason.placeholder = "Reason (optional, recorded on reject)";
        reason.maxLength = 500;
        const approveBtn = el("button", { className: "approve", text: "Approve & run (simulated)" });
        const rejectBtn = el("button", { className: "reject", text: "Reject" });
        approveBtn.type = rejectBtn.type = "button";
        if (!canDecide) {
          approveBtn.disabled = rejectBtn.disabled = true;
          approveBtn.title = rejectBtn.title = "Needs an approver key";
        }
        approveBtn.addEventListener("click", () => decide(p.id, "approve", reason.value, [approveBtn, rejectBtn]));
        rejectBtn.addEventListener("click", () => decide(p.id, "reject", reason.value, [approveBtn, rejectBtn]));
        card.append(el("div", { className: "actions" }, approveBtn, rejectBtn, reason));
      }
      return card;
    }),
  );
}

async function decide(actionId, verb, reason, buttons) {
  const approver = $("approver").value.trim();
  $("decision-error").textContent = "";
  if (!approver) {
    $("decision-error").textContent = "Enter the approver name first.";
    $("approver").focus();
    return;
  }
  localStorage.setItem("ic-approver", approver);
  buttons.forEach((b) => (b.disabled = true));
  try {
    const id = state.current;
    await api(`/api/incidents/${encodeURIComponent(id)}/actions/${encodeURIComponent(actionId)}/${verb}`, {
      method: "POST",
      body: { approver, reason: reason.trim() },
    });
    const tl = await api(`/api/incidents/${encodeURIComponent(id)}/timeline?since=${state.cursor}`);
    appendTimeline(tl.entries);
    state.cursor = tl.next_cursor;
    await refreshDetail();
    refreshList();
  } catch (err) {
    $("decision-error").textContent = err.message;
    buttons.forEach((b) => (b.disabled = false));
  }
}

// ------------------------------------------------------------- postmortem
async function loadPostmortem() {
  const id = state.current;
  try {
    const text = await api(`/api/incidents/${encodeURIComponent(id)}/postmortem`, {
      headers: { Accept: "text/markdown" },
    });
    if (id !== state.current) return;
    const article = $("postmortem");
    article.dataset.raw = text;
    article.replaceChildren(...renderMarkdown(text));
  } catch (err) {
    $("postmortem").replaceChildren(el("p", { className: "muted", text: err.message }));
  }
}

// Minimal Markdown renderer for the postmortem: headings, quotes, lists, task items, tables,
// paragraphs, **bold** and `code`. It builds DOM nodes, so no markup in the source is executed.
function inline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
    const token = m[0];
    out.push(
      token.startsWith("**")
        ? el("strong", { text: token.slice(2, -2) })
        : el("code", { text: token.slice(1, -1) }),
    );
    last = m.index + token.length;
  }
  if (last < text.length) out.push(document.createTextNode(text.slice(last)));
  return out;
}

function splitRow(line) {
  const cells = [];
  let cur = "";
  const body = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  for (let i = 0; i < body.length; i++) {
    if (body[i] === "\\" && body[i + 1] === "|") {
      cur += "|";
      i++;
    } else if (body[i] === "|") {
      cells.push(cur.trim());
      cur = "";
    } else {
      cur += body[i];
    }
  }
  cells.push(cur.trim());
  return cells;
}

function renderMarkdown(src) {
  const lines = src.split("\n");
  const nodes = [];
  let list = null;
  let i = 0;
  const closeList = () => {
    if (list) nodes.push(list);
    list = null;
  };
  while (i < lines.length) {
    const line = lines[i];
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      closeList();
      nodes.push(el(`h${heading[1].length}`, {}, ...inline(heading[2])));
    } else if (line.startsWith("> ")) {
      closeList();
      nodes.push(el("blockquote", {}, ...inline(line.slice(2))));
    } else if (/^\s*- /.test(line)) {
      if (!list) list = el("ul");
      const nested = /^\s{2,}- /.test(line);
      let content = line.replace(/^\s*- /, "");
      const li = el("li");
      const task = /^\[( |x)\] /.exec(content);
      if (task) {
        const box = el("input");
        box.type = "checkbox";
        box.disabled = true;
        box.checked = task[1] === "x";
        li.className = "task";
        li.append(box, " ");
        content = content.slice(4);
      }
      li.append(...inline(content));
      if (nested && list.lastElementChild) {
        let sub = list.lastElementChild.querySelector("ul");
        if (!sub) {
          sub = el("ul");
          list.lastElementChild.append(sub);
        }
        sub.append(li);
      } else {
        list.append(li);
      }
    } else if (line.trim().startsWith("|")) {
      closeList();
      const table = el("table");
      const head = splitRow(line);
      table.append(el("thead", {}, el("tr", {}, ...head.map((c) => el("th", {}, ...inline(c))))));
      const tbody = el("tbody");
      i += 1;
      if (i < lines.length && /^\|[-| :]+\|$/.test(lines[i].trim())) i += 1;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tbody.append(el("tr", {}, ...splitRow(lines[i]).map((c) => el("td", {}, ...inline(c)))));
        i += 1;
      }
      table.append(tbody);
      nodes.push(table);
      continue;
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      nodes.push(el("p", {}, ...inline(line)));
    }
    i += 1;
  }
  closeList();
  return nodes;
}

document.addEventListener("DOMContentLoaded", init);
