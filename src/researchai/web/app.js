// ResearchAI web UI: start a session, stream its events (SSE), render the hero, lifecycle,
// plan, summary, report, sources and activity, and send user controls.

const $ = (sel) => document.querySelector(sel);
const state = {
  id: null, snap: null, source: null, lastSeq: 0, refreshTimer: null, dir: null,
  prevStatus: null, summaryKey: null, flags: null, tab: "plan",
  qa: {}, // per-session question history: [{question, status, answer}]
};
const newFlags = () => ({ reviewed: false, rejections: 0, firstTs: null, lastTs: null, pausedMs: 0, pausedAt: null });

const OWN = "Your direction";
const LOG_LABEL = {
  plan: "Plan", todos: "To-dos", delegate: "Delegate", search: "Search", kept: "Kept",
  dropped: "Dropped", evidence: "Evidence", status: "Review", gap: "Gap", round: "Round",
  question: "Question", answer: "Answer", assumption: "Assumed", control: "You",
  report_rejected: "Rejected", report_accepted: "Accepted", session: "Session",
  memory: "Memory", suggestions: "Next", agent: "Agent", error: "Error",
};

// Lucide icon shapes (ISC licence), drawn inline so no icon library is needed.
const ICONS = {
  compass: '<circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12z"/>',
  plan: '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  review: '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="3"/><path d="m16 16-1.9-1.9"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  report: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V2H6.5A2.5 2.5 0 0 0 4 4.5z"/>',
  go: '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
  panel: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
};

const STAGES = [
  ["Direction", "compass"], ["Plan", "plan"], ["Research", "search"],
  ["Review", "review"], ["Follow-up", "refresh"], ["Report", "report"],
];

// ---------------------------------------------------------------- helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false && child !== "") {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) {
    svg.setAttribute(k, v);
  }
  svg.innerHTML = ICONS[name]; // trusted constant shapes only
  return svg;
}

// Replace an element's children; a no-op if the element isn't on the page, so a missing
// element (e.g. a stale cached page) can never crash the whole UI.
function fill(sel, ...nodes) {
  const node = $(sel);
  if (node) node.replaceChildren(...nodes);
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function toast(message) {
  const t = $("#toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
}

async function control(action, extra = {}) {
  try {
    await api(`/api/sessions/${state.id}/control`, { method: "POST", body: { action, ...extra } });
    scheduleRefresh(0);
  } catch (err) {
    toast(err.message);
  }
}

const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const clock = (secs) => `${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, "0")}`;
const duration = (secs) => (secs >= 60 ? `${Math.floor(secs / 60)} min ${String(secs % 60).padStart(2, "0")} s` : `${secs} s`);
const human = (s) => s.replaceAll("_", " ");

// "united airlines" -> "United airlines"; leaves "iPhone"-style names alone.
const capFirst = (text) => (text && /^\p{Ll}/u.test(text) && !/^.\p{Lu}/u.test(text)
  ? text.charAt(0).toUpperCase() + text.slice(1) : text);

// Capitalise the first letter as the user types, keeping the cursor where it was.
function autoCapitalize(input) {
  input.setAttribute("autocapitalize", "sentences");
  input.addEventListener("input", () => {
    const fixed = capFirst(input.value);
    if (fixed === input.value) return;
    const [start, end] = [input.selectionStart, input.selectionEnd];
    input.value = fixed;
    input.setSelectionRange(start, end);
  });
  return input;
}
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const isActive = (s) => ["starting", "running", "revising", "waiting_for_answer"].includes(s.status) && !s.archived;

// ---------------------------------------------------------------- sessions

async function startSession(topic) {
  try {
    const { id } = await api("/api/sessions", { method: "POST", body: { topic } });
    openSession(id);
    loadSessions();
  } catch (err) {
    toast(err.message);
  }
}

function openSession(id) {
  if (state.source) state.source.close();
  Object.assign(state, { id, snap: null, lastSeq: 0, dir: null, prevStatus: null, summaryKey: null, flags: newFlags() });
  history.replaceState(null, "", `#${id}`);
  renderSidebar();
  closeDrawer();
  $("#start").hidden = true;
  $("#session").hidden = false;
  for (const sel of ["#notes", "#log", "#question"]) fill(sel);
  $("#tab-report").dataset.version = "";
  selectTab("plan");

  state.source = new EventSource(`/api/sessions/${id}/events`);
  state.source.addEventListener("log", (e) => {
    const event = JSON.parse(e.data);
    if (event.seq <= state.lastSeq) return; // replayed after a reconnect
    state.lastSeq = event.seq;
    track(event);
    if (event.kind === "session") loadSessions(); // status changed: update the sidebar
    addLog(event);
    addNote(event);
    scheduleRefresh();
  });
  scheduleRefresh(0);
}

function track(ev) {
  const f = state.flags;
  f.firstTs = f.firstTs || ev.ts;
  f.lastTs = ev.ts;
  if (ev.kind === "status") f.reviewed = true;
  if (ev.kind === "report_rejected") f.rejections += 1;
  // paused time doesn't count towards the elapsed clock
  const action = ev.kind === "control" ? ev.data?.action : null;
  if (action === "pause" && !f.pausedAt) f.pausedAt = ev.ts;
  if (action === "resume" && f.pausedAt) {
    f.pausedMs += new Date(ev.ts) - new Date(f.pausedAt);
    f.pausedAt = null;
  }
}

// ---------------------------------------------------------------- sidebar: all previous research

const SIDEBAR_STATUS = {
  running: ["Researching", "live"], starting: ["Researching", "live"], revising: ["Revising", "live"],
  waiting_for_answer: ["Needs your answer", "attention"], failed: ["Stopped", "stopped"],
};

async function loadSessions() {
  state.sessions = await api("/api/sessions").catch(() => state.sessions || []);
  renderSidebar();
}

function dateGroup(iso) {
  const day = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((day(new Date()) - day(new Date(iso))) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 7) return "Previous 7 days";
  if (days <= 30) return "Previous 30 days";
  return "Older";
}

function renderSidebar() {
  const list = state.sessions || [];
  const query = ($("#search")?.value || "").trim().toLowerCase();
  const shown = query ? list.filter((s) => s.topic.toLowerCase().includes(query)) : list;
  if (!list.length) { fill("#session-list", el("p", { class: "list-empty" }, "Your research will appear here.")); return; }
  if (!shown.length) { fill("#session-list", el("p", { class: "list-empty" }, `No research matches “${query}”.`)); return; }

  const groups = new Map();
  for (const s of shown) {
    const g = dateGroup(s.created);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(s);
  }
  fill("#session-list", ...[...groups].map(([name, items]) => el("div", { class: "list-group" },
    el("p", { class: "list-label" }, name),
    el("ul", {}, items.map((s) => {
      const status = s.paused && SIDEBAR_STATUS[s.status]?.[1] === "live" ? ["Paused", "paused"] : SIDEBAR_STATUS[s.status];
      if (state.confirmDelete === s.id) {
        return el("li", {}, el("div", { class: "list-confirm" },
          el("span", {}, "Delete this research?"),
          el("div", { class: "list-confirm-buttons" },
            el("button", { class: "btn small danger", type: "button", onclick: () => deleteSession(s.id) }, "Delete"),
            el("button", { class: "btn small ghost", type: "button", onclick: () => { state.confirmDelete = null; renderSidebar(); } }, "Cancel"))));
      }
      return el("li", { class: "list-row" }, el("a", {
        class: `list-item${s.id === state.id ? " active" : ""}`, href: `#${s.id}`, title: capFirst(s.topic),
        "aria-current": s.id === state.id ? "page" : null,
        onclick: (e) => { e.preventDefault(); if (s.id !== state.id) openSession(s.id); else closeDrawer(); },
      },
      el("span", { class: "list-topic" }, capFirst(s.topic)),
      status ? el("span", { class: `list-status ${status[1]}` }, status[0]) : ""),
      el("button", {
        class: "list-delete", type: "button", title: "Delete", "aria-label": `Delete “${capFirst(s.topic)}”`,
        onclick: () => { state.confirmDelete = s.id; renderSidebar(); },
      }, icon("trash")));
    })))));
}

async function deleteSession(id) {
  try {
    await api(`/api/sessions/${id}`, { method: "DELETE" });
  } catch (err) {
    toast(err.message);
    return;
  }
  state.confirmDelete = null;
  delete state.qa[id];
  if (id === state.id) showStart();
  toast("Research deleted");
  loadSessions();
}

// Desktop: collapse the sidebar (remembered). Mobile: the sidebar is a drawer.
const mobile = () => window.matchMedia("(max-width: 860px)").matches;
function setCollapsed(collapsed) {
  $("#app").classList.toggle("collapsed", collapsed);
  try { localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0"); } catch { /* storage unavailable */ }
}
function openDrawer() { $("#app").classList.add("drawer-open"); $("#backdrop").hidden = false; }
function closeDrawer() { $("#app").classList.remove("drawer-open"); $("#backdrop").hidden = true; }

function scheduleRefresh(delay = 250) {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(refresh, delay);
}

async function refresh() {
  if (!state.id) return;
  try {
    state.snap = await api(`/api/sessions/${state.id}`);
  } catch (err) {
    toast(err.message === "Unknown session" ? "That session no longer exists." : err.message);
    if (err.message === "Unknown session") showStart();
    return;
  }
  const s = state.snap;
  renderHero();
  renderLifecycle();
  renderQuestion();
  renderPlan();
  renderGaps();
  renderNext();
  renderAsk();
  renderSources();
  renderReport();
  renderSummary();
  if (s.status === "done" && state.prevStatus !== "done") selectTab("summary");
  state.prevStatus = s.status;
}

function showStart() {
  state.source?.close();
  state.id = null;
  history.replaceState(null, "", location.pathname);
  $("#session").hidden = true;
  $("#start").hidden = false;
  renderSidebar();
  closeDrawer();
}

// ---------------------------------------------------------------- start screen

function renderStart() {
  const steps = [
    ["You choose the direction", "Pick the angles and depth that matter to you."],
    ["A plan around your choices", "Three to five focused questions."],
    ["Parallel research", "Recent sources, each kept or dropped for a reason."],
    ["It checks its own evidence", "Weak spots are explained, not hidden."],
    ["Targeted follow-up", "More research only where it's needed."],
    ["A cited report", "Every citation checked before you see it."],
  ];
  fill("#start-steps", ...STAGES.map(([name, ic], i) => el("li", { class: "done" },
    el("span", { class: "circle" }, icon(ic)),
    el("span", { class: "step-name" }, name),
    el("span", { class: "step-sub" }, steps[i][1]))));
}

// ---------------------------------------------------------------- hero + lifecycle

// The status row above the title: a coloured state badge, round progress, and a hint.
function renderStatusRow(s) {
  const following = s.round > 1 || (s.gaps.length > 0 && !s.report);
  const [kind, , label, hint] = s.paused && isActive(s)
    ? ["paused", "pause", "Paused", "Stops at its next step."]
    : {
      starting: ["live", null, "Starting", "Preparing the research."],
      running: ["live", null, "Researching", following ? "Following up weak questions" : s.plan ? "Researching every question in parallel" : "Planning the research"],
      waiting_for_answer: ["attention", "compass", "Needs your answer", "Choose a direction to start."],
      revising: ["live", "refresh", "Revising", "Updating the report after your change."],
      done: s.report ? ["done", "check", "Complete", "Every citation checked."] : ["stopped", "alert", "Finished", "No report was produced."],
      failed: ["stopped", "alert", "Stopped", "The run stopped. See Activity for details."],
    }[s.status] || ["stopped", "alert", s.status, ""];
  const showRounds = s.plan && !(s.status === "done" && s.round === 1);
  const key = [kind, label, hint, showRounds && `${s.round}/${s.max_rounds}`, s.archived].join("|");
  const row = $("#status-row");
  if (!row || row.dataset.key === key) return; // unchanged: skip the redraw
  row.dataset.key = key;

  const rounds = showRounds ? el("div", { class: "status-round" },
    el("span", {}, `Round ${s.round} of ${s.max_rounds}`),
    el("span", { class: "round-bars", "aria-hidden": "true" }, Array.from({ length: s.max_rounds }, (_, i) =>
      el("i", { class: i + 1 <= s.round ? "on" : "" })))) : "";
  row.className = `status-row ${kind}`;
  row.replaceChildren(
    el("div", { class: "status-main" },
      el("p", { class: "status-label" }, label, s.archived ? el("span", { class: "status-saved" }, "Saved session") : ""),
      hint ? el("p", { class: "status-hint" }, hint,
        kind === "paused" ? el("button", { class: "link resume", type: "button", onclick: () => control("resume") }, "Resume") : "") : ""),
    rounds);
}

function renderHero() {
  const s = state.snap;
  renderStatusRow(s);
  $("#title").textContent = capFirst(s.topic);
  document.title = `${capFirst(s.topic)} · ResearchAI`;

  const d = s.direction;
  fill("#direction", ...(d ? [el("span", { class: "label" }, "Your direction"),
    ...Object.entries(d).flatMap(([k, v]) => (k === OWN
      ? [el("span", { class: "tag blue" }, `“${v}”`)]
      : (Array.isArray(v) ? v : [v]).map((x) => el("span", { class: "tag outline" }, x))))] : []));

  const used = s.sources.filter((x) => x.evidence > 0).length;
  const f = state.flags;
  let elapsed = 0;
  if (f.firstTs) {
    const end = f.pausedAt ? new Date(f.pausedAt).getTime() // frozen while paused
      : isActive(s) ? Date.now() : new Date(f.lastTs).getTime();
    elapsed = Math.max(0, (end - new Date(f.firstTs).getTime() - f.pausedMs) / 1000);
  }
  const gapsLabel = s.status === "done" ? "gaps followed up" : "gaps found";
  fill("#stats", ...[[used, "sources used"], [s.gaps.length, gapsLabel], [clock(elapsed), s.status === "done" ? "total time" : "elapsed"]]
    .map(([v, l]) => el("div", {}, el("dt", {}, l), el("dd", {}, String(v)))));

  const pause = $("#pause");
  pause.hidden = (!isActive(s) && !s.paused) || s.status === "waiting_for_answer";
  pause.textContent = s.paused ? "Resume" : "Pause";
}

function stageStates() {
  const s = state.snap;
  const f = state.flags;
  const done = s.status === "done" && s.report;
  const used = s.sources.filter((x) => x.evidence > 0).length;
  const followUp = !done && (s.round > 1 || s.gaps.length > 0); // gaps found → following up
  const st = {};
  st.Direction = s.direction || s.plan ? "done" : "now";
  st.Plan = s.plan ? "done" : s.direction ? "now" : "next";
  st.Research = f.reviewed || done ? "done" : s.plan ? "now" : "next";
  st.Review = f.reviewed || done ? "done" : "next";
  st["Follow-up"] = done ? "done" : followUp ? "now" : "next";
  st.Report = done && s.status !== "revising" ? "done" : s.status === "revising" ? "now"
    : f.reviewed && !followUp && s.gaps.length === 0 ? "now" : "next";
  if (followUp && !done && f.rejections === 0 && st["Follow-up"] === "now") st.Report = "next";
  const subs = {
    Direction: s.direction ? "You chose" : "Choose below",
    Plan: s.plan ? `${s.plan.sub_questions.length} questions` : "",
    Research: used ? `${used} sources` : "",
    Review: f.reviewed ? (s.gaps.length ? `${s.gaps.length} gaps found` : "All well sourced") : "",
    "Follow-up": done ? (s.gaps.length ? `${s.gaps.length} followed up` : "Not needed")
      : followUp ? (s.round > 1 ? `Round ${s.round}` : "Starting") : "",
    Report: s.report ? (s.report_version > 1 ? `Revised · v${s.report_version}` : "Accepted") : (st.Report === "now" ? "Writing" : ""),
  };
  return { st, subs };
}

function renderLifecycle() {
  const { st, subs } = stageStates();
  fill("#lifecycle", ...STAGES.map(([name, ic]) => el("li", { class: st[name] },
    el("span", { class: "circle" }, st[name] === "done" && name !== "Report" ? icon(ic) : icon(ic)),
    el("span", { class: "step-name" }, name),
    el("span", { class: "step-sub" }, subs[name] || "—"))));
  const s = state.snap;
  const now = {
    waiting_for_answer: "Now: waiting for you to choose a direction",
    revising: "Now: revising the report after your change",
    done: "Every citation was checked against its source before acceptance",
    failed: "The run stopped; see Activity for details",
  }[s.status] || (s.paused ? "Paused: the agent stops at its next step"
    : s.round > 1 || (s.gaps.length && !s.report) ? "Now: following up the weak questions before writing"
      : s.plan ? "Now: researching every question in parallel" : "Now: preparing");
  $("#now").textContent = now;
}

// ---------------------------------------------------------------- direction picker

// One tab per question, clickable options, plus a free-text tab. Working state lives in
// state.dir so snapshot refreshes don't wipe the user's picks.
function renderQuestion() {
  const box = $("#question");
  const pending = state.snap.pending_question;
  if (!pending) { box.replaceChildren(); state.dir = null; return; }
  const key = JSON.stringify(pending.questions);
  if (state.dir && state.dir.key === key) return;
  state.dir = { key, questions: pending.questions, tab: 0, picks: {}, own: "" };
  for (const q of pending.questions) state.dir.picks[q.header] = new Set();
  drawDirection();
}

function drawDirection() {
  const d = state.dir;
  const tabs = [...d.questions.map((q) => q.header), OWN];
  const answered = (i) => (i < d.questions.length
    ? d.picks[d.questions[i].header].size > 0 : d.own.trim().length > 0);
  const ready = () => d.own.trim() || d.questions.every((q) => d.picks[q.header].size > 0);
  const goTo = (i) => { d.tab = i; drawDirection(); };

  let panel;
  if (d.tab < d.questions.length) {
    const q = d.questions[d.tab];
    const picks = d.picks[q.header];
    const toggle = (label) => {
      if (q.multi_select) { picks.has(label) ? picks.delete(label) : picks.add(label); drawDirection(); }
      else { picks.clear(); picks.add(label); goTo(Math.min(d.tab + 1, tabs.length - 1)); }
    };
    panel = el("div", { class: "dir-panel", role: "tabpanel" },
      el("div", { class: "dir-question" }, el("strong", {}, q.question),
        el("span", {}, q.multi_select ? "Choose any that apply" : "Choose one")),
      el("div", { class: "dir-options" }, q.options.map((o) => el("button", {
        class: `dir-option${picks.has(o.label) ? " selected" : ""}`, type: "button",
        "aria-pressed": picks.has(o.label) ? "true" : "false", onclick: () => toggle(o.label),
      },
      el("span", { class: "opt-top" },
        el("span", { class: `dir-mark${q.multi_select ? "" : " radio"}` }, picks.has(o.label) ? icon("check") : ""),
        el("span", { class: "dir-label" }, o.label),
        o.recommended ? el("span", { class: "tag green" }, "Recommended") : ""),
      el("span", { class: "dir-desc" }, o.description)))));
  } else {
    const area = autoCapitalize(el("textarea", { placeholder: "Tell the agent what you want from this research: a specific angle, sources to prioritise, what to leave out…" }));
    area.value = d.own;
    area.addEventListener("input", () => { d.own = area.value; $(".dir-footer")?.replaceWith(footer()); });
    panel = el("div", { class: "dir-panel", role: "tabpanel" },
      el("div", { class: "dir-question" }, el("strong", {}, "Anything specific you want?"),
        el("span", {}, "Optional · takes priority over the options")), area);
    setTimeout(() => area.focus(), 0);
  }

  const submit = (answers) => control("answer", { answers });
  const chosen = () => {
    const a = {};
    for (const q of d.questions) if (d.picks[q.header].size) a[q.header] = [...d.picks[q.header]];
    if (d.own.trim()) a[OWN] = d.own.trim();
    return a;
  };
  const recommended = () => Object.fromEntries(d.questions.map((q) =>
    [q.header, q.options.filter((o) => o.recommended).map((o) => o.label)]));

  const next = [["plan", "A plan around your choices", "Three to five questions that follow what you picked."],
    ["search", "Parallel research", "Recent, relevant sources, each kept or dropped for a stated reason."],
    ["review", "It checks its own evidence", "Weak spots are explained, then researched again."],
    ["report", "A cited report", "Every citation is checked before you see it."]];

  // Back / Next on each question; Start research on the last ("Your direction") step.
  function footer() {
    const total = d.questions.length;
    const last = d.tab === tabs.length - 1;
    const current = last ? null : d.questions[d.tab];
    const picked = current ? d.picks[current.header].size : 0;
    const missing = d.own.trim() ? [] : d.questions.filter((q) => !d.picks[q.header].size);
    const n = d.questions.reduce((sum, q) => sum + d.picks[q.header].size, 0);
    let note;
    if (!last) {
      note = picked ? `Question ${d.tab + 1} of ${total} · ${picked} selected` : "Choose at least one option to continue";
    } else if (missing.length) {
      const i = d.questions.indexOf(missing[0]);
      note = ["Answer ", el("button", { class: "link", type: "button", onclick: () => goTo(i) }, missing[0].header), " to continue"];
    } else {
      note = n ? `${n} selected${d.own.trim() ? " · plus your own direction" : ""}` : "Using your own direction";
    }
    return el("div", { class: "dir-footer" },
      el("span", { class: "dir-note" }, note),
      el("div", { class: "buttons" },
        el("button", { class: "link", type: "button", onclick: () => submit(recommended()) }, "Use recommended"),
        d.tab > 0 ? el("button", { class: "btn", type: "button", onclick: () => goTo(d.tab - 1) }, "Back") : "",
        last
          ? el("button", { id: "dir-start", class: "btn primary", type: "button", disabled: !ready(), onclick: () => submit(chosen()) }, "Start research")
          : el("button", { class: "btn primary", type: "button", disabled: !picked, onclick: () => goTo(d.tab + 1) }, "Next")));
  }

  fill("#question", el("div", { class: "dir-backdrop" },
    el("div", { class: "dir-dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "dir-title" },
      el("div", { class: "dir-main" },
        el("p", { class: "eyebrow" }, "New research · step 1"),
        el("h2", { id: "dir-title" }, "Before I start, which direction should this research take?"),
        el("p", { class: "dir-topic" }, capFirst(state.snap.topic)),
        el("div", { class: "dir-tabs", role: "tablist" }, tabs.map((t, i) => el("button", {
          class: "dir-tab", role: "tab", type: "button", "aria-selected": i === d.tab ? "true" : "false",
          onclick: () => goTo(i),
        }, t, answered(i) ? el("span", { class: "dir-check" }, " ✓") : ""))),
        panel,
        footer()),
      el("div", { class: "dir-aside" },
        el("h3", {}, "What happens next"),
        el("ol", {}, next.map(([ic, t, dsc]) => el("li", {},
          el("span", { class: "dot" }, icon(ic)), el("div", {}, el("strong", {}, t), el("span", {}, dsc)))))))));
}

// ---------------------------------------------------------------- plan

function renderPlan() {
  const s = state.snap;
  const box = $("#tab-plan");
  if (!s.plan) {
    box.replaceChildren(el("p", { class: "empty" }, s.pending_question
      ? "Choose a direction and the plan will appear here." : "The plan appears once you've chosen a direction."));
    return;
  }
  const todoFor = (n) => s.todos.find((t) => new RegExp(`^Q${n}\\b`).test(t.content));
  const canSkip = ["running", "revising"].includes(s.status) && !s.archived;

  const rows = s.questions.map((q) => {
    const gaps = s.gaps.filter((g) => g.question_number === q.number);
    const open = gaps.length && s.status !== "done";
    const todo = todoFor(q.number);
    const status = q.skipped ? ["Skipped", "tag"]
      : open ? ["Following up", "tag blue"]
        : todo?.status === "completed" || s.status === "done" ? ["Done", "tag green"]
          : todo?.status === "in_progress" ? ["Researching", "tag blue"] : ["Queued", "tag"];
    const tags = [`${q.sources} source${q.sources === 1 ? "" : "s"}`, `${q.sites} site${q.sites === 1 ? "" : "s"}`];
    if (q.newest) tags.push(`Newest ${new Date(q.newest).toLocaleDateString([], { month: "short", year: "numeric" })}`);
    return el("div", { class: `qrow${q.skipped ? " skipped" : ""}${gaps.length ? " gap" : ""}` },
      el("span", { class: "qbadge" }, `Q${q.number}`),
      el("div", {},
        el("p", { class: "qtext" }, q.question),
        el("div", { class: "tags" }, tags.map((t) => el("span", { class: "tag" }, t)),
          q.flags.map((f) => el("span", { class: "tag orange" }, cap(human(f)))),
          gaps.filter((g) => !q.flags.includes(g.problem)).map((g) => el("span", { class: "tag orange" }, cap(human(g.problem)))))),
      el("div", { class: "qstatus" },
        el("span", { class: status[1] }, status[0]),
        !q.skipped && status[0] !== "Done" ? el("button", {
          class: "link", type: "button", disabled: !canSkip, title: "Stop researching this question",
          onclick: () => control("skip", { question_number: q.number }),
        }, "Skip question") : ""));
  });

  const steps = s.todos.filter((t) => !/^Q\d+\b/.test(t.content));
  box.replaceChildren(
    el("div", { class: "plan-head" },
      el("div", {}, el("p", { class: "eyebrow" }, "Research plan"),
        el("h2", {}, `${s.questions.length} questions${s.direction ? ", shaped by your direction" : ""}`)),
      el("span", { class: "muted small" }, s.plan.time_sensitive
        ? `Sources from the last ${Math.round(s.plan.horizon_days / 30)} months` : "Timeless topic: older sources allowed")),
    ...rows,
    steps.length ? el("div", { class: "todo-steps" }, steps.map((t) => el("span", { class: t.status }, t.content))) : "");
}

// ---------------------------------------------------------------- side cards

function renderGaps() {
  const s = state.snap;
  const box = $("#gaps");
  const done = s.status === "done";
  const byNum = Object.fromEntries(s.questions.map((q) => [q.number, q]));
  const measure = (m) => (m ? `${m.sources} source${m.sources === 1 ? "" : "s"} · ${m.sites} site${m.sites === 1 ? "" : "s"}` : null);
  const rej = state.flags.rejections;

  if (!s.gaps.length) {
    // Show the actual measurements per question; nothing to show before there's a plan.
    box.hidden = !s.plan;
    if (!s.plan) return;
    const month = (d) => new Date(d).toLocaleDateString([], { month: "short", year: "numeric" });
    const rows = s.questions.filter((q) => !q.skipped).map((q) => {
      const weak = (...flags) => (flags.some((f) => q.flags.includes(f)) ? "weak" : "");
      return el("tr", {},
        el("td", {}, `Q${q.number}`),
        el("td", { class: q.sources ? weak("thin", "missing") : "muted" }, q.sources ? `${q.sites} site${q.sites === 1 ? "" : "s"}` : "—"),
        el("td", { class: q.sources ? weak("outdated", "undated") : "muted" }, q.newest ? month(q.newest) : q.sources ? "No date" : "—"),
        el("td", { class: q.quotes_total ? weak("weak_quotes") : "muted" }, q.quotes_total ? `${q.quotes_verified}/${q.quotes_total}` : "—"));
    });
    box.replaceChildren(
      el("h3", { class: "side-title" }, "Evidence check"),
      el("p", { class: "side-sub" }, state.flags.reviewed ? "Every question passed the review." : "Reviewed after the first round. Orange means too weak."),
      el("table", { class: "ev-table" },
        el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "Sites"), el("th", {}, "Newest"), el("th", {}, "Quotes matched"))),
        el("tbody", {}, rows)),
      rej ? el("p", { class: "side-note" }, `The quality checks sent back ${rej} draft${rej > 1 ? "s" : ""} before accepting the report.`) : "");
    return;
  }
  box.hidden = false;
  box.replaceChildren(
    el("h3", { class: "side-title" }, done ? "How the agent adapted" : "Why it's digging deeper"),
    el("p", { class: "side-sub" }, done
      ? `${s.gaps.length} weak spot${s.gaps.length > 1 ? "s" : ""}, followed up`
      : `${s.gaps.length} question${s.gaps.length > 1 ? "s" : ""} need more evidence`),
    ...[...s.gaps].sort((x, y) => x.question_number - y.question_number).map((g) => {
      const q = byNum[g.question_number];
      const stillOpen = q && q.flags.includes(g.problem);
      const status = !done ? ["Following up", "tag blue"] : stillOpen ? ["Still open", "tag orange"] : ["Closed", "tag green"];
      const before = measure(g.before);
      return el("div", { class: "gap-item" },
        el("div", { class: "gap-head" }, el("span", { class: "gap-name" }, `Q${g.question_number} · ${cap(human(g.problem))}`),
          el("span", { class: status[1] }, status[0])),
        el("p", {}, g.why_it_matters),
        done && before ? el("div", { class: "before-after" }, el("span", { class: "tag" }, before),
          el("span", { class: "arrow" }, "→"), el("span", { class: "tag blue" }, measure(q)))
          : el("p", { class: "follow" }, `${done ? "Followed up with" : "Searching"}: ${g.follow_up_queries.join(" · ")}`));
    }),
    rej ? el("p", { class: "side-note" }, `The quality checks sent back ${rej} draft${rej > 1 ? "s" : ""} before accepting the report.`) : "");
}

function renderNext() {
  const s = state.snap;
  const box = $("#next");
  box.hidden = !s.suggestions.length;
  if (!s.suggestions.length) return;
  box.replaceChildren(
    el("h3", { class: "side-title" }, "Research next"),
    ...s.suggestions.map((x) => el("button", { class: "suggestion", type: "button", title: x.why,
      onclick: () => startSession(x.topic) },
    el("span", {}, x.topic, el("small", {}, x.why)), icon("go"))));
}

// ---------------------------------------------------------------- ask about this research
//
// UI only for now. It posts to POST /api/sessions/{id}/ask {question}; until the answering
// engine exists that route returns 404/405 and the turn says so honestly.
// Expected response once connected:
//   {answer_md, used_sources: ["S3", ...], web_sources: [{id, title, url, domain}], searched: [queries]}
// and GET /api/sessions/{id} will then include qa: [...] for the stored history.

const ASK_TABS = new Set(["summary", "report"]);

function updateSide() {
  const s = state.snap;
  const showAsk = Boolean(s && s.report && ASK_TABS.has(state.tab));
  $("#ask").hidden = !showAsk;
  $("#notes-card").hidden = showAsk;
  if (showAsk && $(".side").firstElementChild !== $("#ask")) $(".side").prepend($("#ask"));
}

// 2-3 starter questions from the report's disagreements, plus one general one.
function starterQuestions(report) {
  const section = (title) => {
    const m = report.match(new RegExp(`^## ${title}\\s*$([\\s\\S]*?)(?=^## |$(?![\\s\\S]))`, "im"));
    return m ? m[1] : "";
  };
  const topics = [...section("Where sources disagree").matchAll(/^- \*\*(.+?)[.:]?\*\*/gm)].map((m) => m[1].replace(/[.:]$/, ""));
  const qs = topics.slice(0, 2).map((t) => `Why do the sources disagree on ${t.charAt(0).toLowerCase()}${t.slice(1)}?`);
  qs.push("Which finding is the least certain, and why?");
  return qs;
}

function renderAsk() {
  const s = state.snap;
  updateSide();
  if (!s || !s.report) return;
  const turns = state.qa[state.id] || [];
  const busy = turns.some((t) => t.status === "sending");

  const turnNode = (t) => {
    let body;
    if (t.status === "sending") body = el("p", { class: "ask-status" }, "Looking through the sources…");
    else if (t.status === "unavailable") body = el("p", { class: "ask-status" }, "Answers aren't connected yet. This panel is ready for the answering engine.");
    else if (t.status === "error") body = el("p", { class: "ask-status error" }, t.error);
    else {
      const a = t.answer;
      body = el("div", {},
        mdNode("div", a.answer_md || "", { class: "ask-answer" }),
        a.searched?.length ? el("p", { class: "ask-note" }, `Not covered by the research, so it searched the web for: ${a.searched.join(" · ")}`) : "",
        a.web_sources?.length ? el("ol", { class: "ask-web" }, a.web_sources.map((w) => el("li", {},
          el("span", { class: "cite web" }, w.id), " ",
          el("a", { href: w.url, target: "_blank", rel: "noopener noreferrer" }, w.title),
          el("span", { class: "muted small" }, ` · ${w.domain}`)))) : "");
    }
    return el("li", { class: "ask-turn" }, el("p", { class: "ask-q" }, t.question), body);
  };

  const input = autoCapitalize(el("textarea", { class: "ask-input", rows: 1, placeholder: "Ask a question about the findings…", disabled: busy }));
  const send = () => { const q = input.value.trim(); if (q) askQuestion(q); };
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });

  fill("#ask",
    el("h3", { class: "side-title" }, "Ask about this research"),
    el("p", { class: "side-note ask-hint" }, "Questions are answered from this session's sources first."),
    turns.length
      ? el("ol", { class: "ask-history" }, turns.map(turnNode))
      : el("div", { class: "ask-starters" }, el("p", { class: "ask-starters-label" }, "Try asking"),
        starterQuestions(s.report).map((q) => el("button", { class: "ask-starter", type: "button", onclick: () => askQuestion(q) }, q))),
    el("form", { class: "ask-form", onsubmit: (e) => { e.preventDefault(); send(); } },
      el("label", { class: "visually-hidden", for: "ask-input" }, "Your question"),
      Object.assign(input, { id: "ask-input" }),
      el("button", { class: "btn primary", type: "submit", disabled: busy }, "Ask")));
  const history = $("#ask .ask-history");
  if (history) history.scrollTop = history.scrollHeight;
}

async function askQuestion(question) {
  const id = state.id;
  const turn = { question, status: "sending" };
  (state.qa[id] ||= []).push(turn);
  renderAsk();
  try {
    const res = await fetch(`/api/sessions/${id}/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 405 || (res.status === 404 && data.detail === "Not Found")) turn.status = "unavailable";
    else if (!res.ok) Object.assign(turn, { status: "error", error: data.detail || `Something went wrong (${res.status}).` });
    else Object.assign(turn, { status: "answered", answer: data });
  } catch {
    Object.assign(turn, { status: "error", error: "Couldn't reach the server." });
  }
  if (state.id === id) renderAsk();
}

// The "What the agent is doing" timeline: the narrative events, in plain language.
function addNote(ev) {
  const d = ev.data || {};
  const note = (cls, ic, ...body) => {
    const list = $("#notes");
    list.append(el("li", { class: cls }, el("span", { class: "dot" }, icon(ic)),
      el("div", {}, el("div", { class: "note-text" }, ...body), el("div", { class: "note-time" }, time(ev.ts)))));
    list.scrollTop = list.scrollHeight;
  };
  switch (ev.kind) {
    case "question": note("", "compass", "Asked you to choose a direction"); break;
    case "answer": note("user", "user", ev.message); break;
    case "assumption": note("", "compass", ev.message); break;
    case "memory": note("", "book", "Building on your earlier research",
      el("ul", {}, (d.related || []).map((r) => el("li", {}, capFirst(r.topic))))); break;
    case "plan": note("", "plan", `Planned ${d.plan.sub_questions.length} questions`); break;
    case "status": note("", "review", ev.message.replace(/^Round (\d+)\/\d+ review: /, "Reviewed the evidence: ")); break;
    case "gap": note("gap", "alert", `Gap in Q${d.question_number}: ${d.why_it_matters}`); break;
    case "round": note("", "refresh", ev.message); break;
    case "report_rejected": note("bad", "x", "The quality checks sent a draft back",
      el("ul", {}, (d.problems || []).map((p) => el("li", {}, p)))); break;
    case "report_accepted": note("good", "check", ev.message); break;
    case "control": note("user", "user", ev.message.replace(/^User /, "You ")); break;
    case "agent": note("", "report", ev.message); break;
    case "error": note("bad", "alert", ev.message); break;
    default: break;
  }
}

// ---------------------------------------------------------------- activity (full log)

function addLog(ev) {
  const kind = ev.agent === "user" ? "user" : ev.kind;
  const who = ev.agent === "researcher" ? "Researcher: " : "";
  $("#log").append(el("li", { class: kind },
    el("span", { class: "t" }, time(ev.ts)),
    el("span", { class: "k" }, LOG_LABEL[ev.kind] || ev.kind),
    el("span", { class: "m" }, who + ev.message)));
  $("#log-count").textContent = ev.seq;
}

// ---------------------------------------------------------------- sources

function renderSources() {
  const s = state.snap;
  const box = $("#tab-sources");
  if (!s.sources.length) return;
  const used = s.sources.filter((x) => x.evidence > 0);
  const other = s.sources.filter((x) => x.evidence === 0);
  $("#src-count").textContent = used.length || "";
  const canExclude = !s.archived && !["waiting_for_answer", "revising"].includes(s.status);

  const card = (x) => el("div", { class: `source${x.excluded ? " excluded" : ""}`, id: `src-${x.id}` },
    el("div", { class: "top" },
      el("span", { class: "cite" }, x.id),
      el("a", { class: "title", href: x.url, target: "_blank", rel: "noopener noreferrer" }, x.title),
      x.excluded ? el("span", { class: "tag orange" }, "Excluded")
        : el("button", { class: "link", type: "button", disabled: !canExclude,
          title: "Remove this source from the evidence and the report",
          onclick: () => control("exclude", { source_id: x.id }) }, "Exclude")),
    el("p", { class: "meta" },
      [x.domain, x.published ? new Date(x.published).toLocaleDateString() : "Date unknown",
        x.questions.map((n) => `Q${n}`).join(", "), x.cited ? "Cited in the report" : null].filter(Boolean).join(" · ")
      + `. Kept because: ${x.reasons.join(", ") || "relevant to the question"}.`));

  box.replaceChildren(
    el("p", { class: "muted small" }, `${used.length} sources used as evidence · ${other.length} other candidates kept · ${s.dropped} dropped by triage`),
    ...used.map(card),
    other.length ? el("details", {}, el("summary", {}, `Other candidates (${other.length})`), ...other.map(card)) : "");
}

function showSource(id) {
  selectTab("sources");
  const node = document.getElementById(`src-${id}`);
  if (!node) return;
  const details = node.closest("details");
  if (details) details.open = true;
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  node.classList.add("flash");
  setTimeout(() => node.classList.remove("flash"), 1500);
}

// ---------------------------------------------------------------- markdown (report + summary)

// Markdown -> sanitised HTML, with [S3] / [S1, S4] turned into clickable source chips.
function markdownHtml(md, inline = false) {
  const excluded = new Set((state.snap?.sources || []).filter((x) => x.excluded).map((x) => x.id));
  const withChips = md.replace(/\[(S\d+(?:\s*,\s*S\d+)*)\](?!\()/g, (_, group) =>
    group.split(/\s*,\s*/).map((id) =>
      `<span class="cite${excluded.has(id) ? " excluded" : ""}" data-sid="${id}">${id}</span>`).join(""));
  return DOMPurify.sanitize(inline ? marked.parseInline(withChips) : marked.parse(withChips),
    { ADD_ATTR: ["target"] });
}

function mdNode(tag, md, attrs = {}, inline = false) {
  const node = el(tag, attrs);
  node.innerHTML = markdownHtml(md, inline);
  return node;
}

function renderReport() {
  const s = state.snap;
  const box = $("#tab-report");
  const excludedIds = s.sources.filter((x) => x.excluded).map((x) => x.id).join(",");
  const key = `${s.report_version}:${s.status}:${excludedIds}`;
  if (box.dataset.version === key) return; // avoid re-rendering (and losing scroll) needlessly
  box.dataset.version = key;
  if (!s.report) {
    box.replaceChildren(el("p", { class: "empty" }, s.status === "failed"
      ? "The run stopped; see Activity for details."
      : "The report appears here when it passes the citation and evidence checks."));
    return;
  }
  const article = el("article", { class: "report" });
  article.innerHTML = markdownHtml(s.report);
  article.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
  const banners = [];
  if (s.status === "revising") banners.push(el("div", { class: "banner" }, "Revising the report after your change…"));
  else if (s.report_version > 1) banners.push(el("div", { class: "banner" }, `Revised report (version ${s.report_version})`));
  box.replaceChildren(...banners, article);
}

// ---------------------------------------------------------------- summary

async function renderSummary() {
  const box = $("#tab-summary");
  let sm;
  try { sm = await api(`/api/sessions/${state.id}/summary`); } catch { return; }
  const s = state.snap;
  const key = `${state.id}:${s.status}:${JSON.stringify(sm)}`;
  if (state.summaryKey === key) return; // unchanged: keep the reader's scroll position
  state.summaryKey = key;
  const o = sm.overview, f = sm.findings, a = sm.adaptation, src = sm.sources;
  const done = s.status === "done";
  const section = (title, ...body) => el("section", { class: "sum-section" }, el("h3", {}, title), ...body);
  const pending = (text) => el("p", { class: "muted" }, text);

  const facts = [
    ["Took", duration(o.duration_s)], ["Research rounds", `${o.rounds_used} of ${o.max_rounds}`],
    ["Questions", o.sub_questions], ["Sources used", o.sources_used],
    ["Report", o.report_version > 1 ? `Revised (v${o.report_version})` : o.report_version ? "Accepted" : "Not yet"],
  ];
  const findings = [];
  if (f.executive_summary) findings.push(mdNode("div", f.executive_summary, { class: "sum-lede" }));
  if (f.takeaways.length) findings.push(el("h4", {}, "Key takeaways"), el("ul", { class: "sum-list" }, f.takeaways.map((t) => mdNode("li", t, {}, true))));
  if (f.disagreements) findings.push(el("h4", {}, "Where sources disagree"), mdNode("div", f.disagreements));
  if (f.open_questions) findings.push(el("h4", {}, "Open questions"), mdNode("div", f.open_questions));
  const rejected = a.rejected_drafts.length;
  const dir = sm.direction.answers;

  box.replaceChildren(
    el("div", { class: "sum-head" },
      el("dl", { class: "sum-facts" }, facts.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, String(v))))),
      el("div", { class: "sum-actions" },
        el("a", { class: "btn small", href: `/api/sessions/${state.id}/summary.md`, download: "" }, icon("download"), "Download summary"),
        s.report ? el("button", { class: "btn small ghost", type: "button", onclick: () => selectTab("report") }, "Full report") : "")),

    section("What we found", ...(findings.length ? findings : [pending(done ? "No report was produced." : "Findings appear when the report is accepted.")])),

    section("Your direction", dir
      ? el("ul", { class: "sum-list" }, Object.entries(dir).map(([k, v]) =>
        el("li", {}, el("strong", {}, `${k}: `), Array.isArray(v) ? v.join(", ") : v)))
      : pending("No direction was chosen for this session."),
    sm.direction.assumed ? el("p", { class: "muted small" }, "Nobody answered, so the agent used its recommended options.") : ""),

    section("Plan and evidence", sm.questions.length ? el("table", { class: "sum-table" },
      el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "Question"), el("th", {}, "Evidence"), el("th", {}, "Status"))),
      el("tbody", {}, sm.questions.map((q) => el("tr", { class: q.skipped ? "skipped" : "" },
        el("td", { class: "num" }, `Q${q.number}`), el("td", {}, q.question),
        el("td", {}, q.measure, q.flags.length ? el("div", { class: "flag" }, cap(human(q.flags.join(", ")))) : ""),
        el("td", {}, cap(human(q.status)))))))
      : pending("The plan appears once you've chosen a direction.")),

    section("How the agent adapted",
      a.gaps.length ? el("table", { class: "sum-table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Gap"), el("th", {}, "Why it mattered"), el("th", {}, "Before → after"))),
        el("tbody", {}, a.gaps.map((g) => el("tr", {},
          el("td", {}, el("strong", {}, `Q${g.question_number}`), ` ${human(g.problem)}`, g.still_open ? el("div", { class: "flag" }, "Still open") : ""),
          el("td", {}, g.why_it_matters),
          el("td", { class: "small" }, g.before, " → ", g.after)))))
        : pending(done ? "No evidence gaps needed follow-up research." : "The agent reviews its evidence after the first round."),
      rejected ? el("p", {}, `The quality checks sent back ${rejected} draft${rejected > 1 ? "s" : ""} before accepting the report.`)
        : done ? el("p", {}, "The first draft passed the citation and evidence checks.") : ""),

    section("Sources",
      el("p", {}, `${src.used} used as evidence · ${src.candidates} other candidates kept · ${src.dropped} dropped`
        + (Object.keys(src.dropped_by_reason).length ? ` (${Object.entries(src.dropped_by_reason).map(([k, v]) => `${v} ${k}`).join(", ")})` : "")),
      src.top_cited.length ? el("ol", { class: "sum-list" }, src.top_cited.map((t) => el("li", {},
        el("span", { class: "cite", "data-sid": t.id }, t.id), " ",
        el("a", { href: t.url, target: "_blank", rel: "noopener noreferrer" }, t.title),
        el("span", { class: "muted small" }, ` · ${t.domain}, cited ${t.count}×`)))) : "",
      src.excluded.length ? el("p", { class: "muted" }, `Excluded by you: ${src.excluded.map((x) => `${x.id} (${x.domain})`).join(", ")}`) : ""),

    section("Your actions", sm.actions.length
      ? el("ul", { class: "sum-list" }, sm.actions.map((x) => el("li", {}, el("span", { class: "muted small" }, `${time(x.time)} · `), x.text)))
      : pending("You didn't need to step in during this session.")),

    sm.context.assumptions.length ? section("Assumptions", el("ul", { class: "sum-list" }, sm.context.assumptions.map((x) => el("li", {}, x)))) : "",
    sm.context.related.length ? section("Built on earlier research", el("ul", { class: "sum-list" }, sm.context.related.map((r) =>
      el("li", {}, el("a", { href: `#${r.id}`, onclick: (e) => { e.preventDefault(); openSession(r.id); } }, capFirst(r.topic)))))) : "",
  );
}

// ---------------------------------------------------------------- tabs + wiring

function selectTab(name) {
  state.tab = name;
  updateSide();
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => { t.hidden = t.id !== `tab-${name}`; });
}

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
for (const id of ["#tab-summary", "#tab-report", "#ask"]) {
  $(id).addEventListener("click", (e) => {
    const cite = e.target.closest(".cite");
    if (cite) showSource(cite.dataset.sid);
  });
}
$("#start-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const topic = $("#topic").value.trim();
  if (topic) startSession(topic);
});
$("#pause").addEventListener("click", () => control(state.snap.paused ? "resume" : "pause"));
$("#new").addEventListener("click", (e) => { e.preventDefault(); showStart(); $("#topic").focus(); });
autoCapitalize($("#search")).addEventListener("input", renderSidebar);
autoCapitalize($("#topic"));
$("#collapse").append(icon("panel"));
$("#open-sidebar").append(icon("panel"));
$("#collapse").addEventListener("click", () => (mobile() ? closeDrawer() : setCollapsed(true)));
$("#open-sidebar").addEventListener("click", () => (mobile() ? openDrawer() : setCollapsed(false)));
$("#backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
try { if (localStorage.getItem("sidebar-collapsed") === "1") setCollapsed(true); } catch { /* storage unavailable */ }
// keep the sidebar current while anything is still researching
setInterval(() => { if ((state.sessions || []).some((s) => SIDEBAR_STATUS[s.status]?.[1] === "live")) loadSessions(); }, 15000);
setInterval(() => { if (state.snap && isActive(state.snap)) renderHero(); }, 1000); // live elapsed clock

renderStart();
loadSessions();
if (location.hash.length > 1) openSession(location.hash.slice(1));
