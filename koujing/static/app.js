"use strict";

const CATEGORIES = [
  { key: "consistent", label: "一致", color: "#0b6f6a", hint: "表、字段、聚合、过滤与粒度全部吻合" },
  { key: "drift", label: "偏差", color: "#946200", hint: "聚合、过滤、表或粒度与字典不一致" },
  { key: "missing_field", label: "缺失字段", color: "#9a4b14", hint: "报表字段与字典声明不能互相覆盖" },
  { key: "unregistered", label: "未登记", color: "#a1323d", hint: "报表聚合别名不在口径字典中" }
];

const state = { batches: [], selected: "", detail: null };

const byId = (id) => document.getElementById(id);

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function showToast(message) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.hidden = true; }, 3800);
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`请求失败 (${response.status}) ${detail.slice(0, 160)}`);
  }
  return response.json();
}

function renderBatches() {
  const container = byId("batches");
  container.replaceChildren();
  byId("batch-count").textContent = state.batches.length;
  if (!state.batches.length) {
    const empty = make("p", "placeholder", "暂无批次。运行 check 后刷新。");
    container.append(empty);
    return;
  }
  for (const batch of state.batches) {
    const button = make("button", "batch-button");
    button.type = "button";
    button.dataset.batch = batch.batch_id;
    if (batch.batch_id === state.selected) button.classList.add("active");
    const title = make("span", "", batch.batch_id);
    const counts = batch.counts || {};
    const meta = make(
      "small", "",
      `${batch.created_at} · ${batch.report_count} 份报表 · 偏差 ${counts.drift || 0}`
    );
    button.append(title, meta);
    button.addEventListener("click", () => selectBatch(batch.batch_id));
    container.append(button);
  }
}

async function loadBatches() {
  try {
    const data = await getJson("/api/batches");
    state.batches = data.batches || [];
    renderBatches();
    if (state.batches.length) {
      await selectBatch(state.selected || state.batches[0].batch_id);
    } else {
      byId("empty").hidden = false;
      byId("detail").hidden = true;
    }
  } catch (error) {
    showToast(error.message);
    byId("batches").replaceChildren(make("p", "placeholder", "批次读取失败"));
  }
}

function summaryStats(detail) {
  const container = byId("summary");
  container.replaceChildren();
  const counts = detail.summary || {};
  for (const category of CATEGORIES) {
    const card = make("div", "stat");
    card.style.setProperty("--stat", category.color);
    const count = make("strong", "", counts[category.key] || 0);
    count.style.color = category.color;
    card.append(count, make("span", "", category.label));
    card.title = category.hint;
    container.append(card);
  }
}

function findingCard(finding, index) {
  const article = make("article", "finding");
  const text = make("div");
  const heading = make("h4", "", `${finding.report} → ${finding.metric}`);
  const summary = make("p", "", finding.summary || "");
  text.append(heading, summary);
  const button = make("button", "evidence-button", "查看证据");
  button.type = "button";
  button.addEventListener("click", () => openEvidence(index));
  const foot = make(
    "div", "finding-foot",
    `第 ${finding.line_start}–${finding.line_end} 行 · 解析器 ${finding.parser_source} · ` +
    `${(finding.differences || []).length} 项差异`
  );
  article.append(text, button, foot);
  return article;
}

function renderGroups(detail) {
  const container = byId("groups");
  container.replaceChildren();
  const findings = detail.findings || [];
  for (const category of CATEGORIES) {
    const group = make("section", "group");
    group.dataset.category = category.key;
    const head = make("div", "group-head");
    head.append(
      make("h3", "", category.label),
      make("span", "pill", findings.filter((item) => item.category === category.key).length)
    );
    group.append(head);
    const items = findings
      .map((finding, index) => ({ finding, index }))
      .filter((entry) => entry.finding.category === category.key);
    if (!items.length) {
      group.append(make("p", "none", category.hint));
    } else {
      for (const entry of items) group.append(findingCard(entry.finding, entry.index));
    }
    container.append(group);
  }
}

function renderDetail(detail) {
  byId("empty").hidden = true;
  byId("detail").hidden = false;
  const batch = detail.batch;
  byId("batch-kicker").textContent = "BATCH · " + batch.parser_mode + " parser";
  byId("batch-title").textContent = batch.batch_id;
  byId("batch-meta").textContent =
    `${batch.created_at} · ${batch.report_count} 份报表 · 字典指纹 ${batch.dict_fingerprint.slice(0, 16)}…`;
  byId("download").href = `/api/batches/${encodeURIComponent(batch.batch_id)}/report.json`;
  summaryStats(detail);
  renderGroups(detail);
}

async function selectBatch(batchId) {
  state.selected = batchId;
  renderBatches();
  try {
    const detail = await getJson(`/api/batches/${encodeURIComponent(batchId)}`);
    state.detail = detail;
    renderDetail(detail);
    byId("main").focus({ preventScroll: true });
  } catch (error) {
    showToast(error.message);
  }
}

async function openEvidence(index) {
  if (!state.detail) return;
  try {
    const batchId = state.detail.batch.batch_id;
    const data = await getJson(
      `/api/batches/${encodeURIComponent(batchId)}/findings/${index + 1}`
    );
    byId("drawer-content").textContent = JSON.stringify(data.finding, null, 2);
    byId("drawer").hidden = false;
    byId("drawer").setAttribute("aria-hidden", "false");
    byId("drawer-close").focus();
  } catch (error) {
    showToast(error.message);
  }
}

function closeDrawer() {
  byId("drawer").hidden = true;
  byId("drawer").setAttribute("aria-hidden", "true");
}

byId("drawer-close").addEventListener("click", closeDrawer);
byId("drawer").addEventListener("click", (event) => {
  if (event.target.dataset.close === "1") closeDrawer();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !byId("drawer").hidden) closeDrawer();
});

loadBatches();
