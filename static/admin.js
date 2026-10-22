const $ = (id) => document.getElementById(id);
const loadVersions = new Map();
const pendingActions = new Set();
let feedbackVersion = 0;
let authRedirectStarted = false;
let showcaseMode = false;
let showcaseTotals = null;
const REVIEW_PAGE_SIZE = 50;
let reviewPage = 0;
let reviewRows = [];

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    if (!authRedirectStarted) {
      authRedirectStarted = true;
      $("admin-error").textContent = "登录已过期，请重新登录。";
      window.location.replace("/admin/login?next=%2Fadmin&expired=1");
    }
    throw new Error("登录已过期，请重新登录。");
  }
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）`);
  return body;
}
function node(tag, text = "", cls = "") {
  const element = document.createElement(tag); element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function numberText(value, places = 1) { return typeof value === "number" && Number.isFinite(value) ? value.toFixed(places) : "—"; }
function percentText(value) { return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—"; }
const beijingFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
function beijingTimeText(value) {
  // Require an explicit offset so an older or malformed value cannot acquire the browser's timezone.
  const iso = typeof value === "string" && value.match(/^(\d{4}-\d{2}-\d{2})T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/);
  if (!iso) return "—";
  const date = new Date(value);
  const calendarDate = new Date(`${iso[1]}T00:00:00Z`);
  if (!Number.isFinite(date.getTime()) || !Number.isFinite(calendarDate.getTime())
      || calendarDate.toISOString().slice(0, 10) !== iso[1]) return "—";
  const parts = Object.fromEntries(beijingFormatter.formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}
function durationText(seconds) {
  return typeof seconds === "number" && Number.isFinite(seconds) && seconds >= 0 ? `${seconds.toFixed(1)} 秒` : "—";
}
function bandText(band) {
  const [low, high] = String(band).split("-").map(Number);
  return Number.isFinite(low) && Number.isFinite(high) ? (high === 100 ? `${low}–100` : `${low}–<${high}`) : String(band || "—");
}

function aiPromptType(message) {
  if (message.role !== "assistant") return "";
  if (["module_clarification", "prompt_clarification"].includes(message.action)) return "澄清";
  return message.action === "task_guidance" ? "引导" : "";
}

function aiPromptCounts(row) {
  const messages = Array.isArray(row.messages) ? row.messages : [];
  const declaredOr = (field, fallback) => Number.isInteger(row[field]) && row[field] >= 0 ? row[field] : fallback;
  const clarification = declaredOr("ai_clarification_count", messages.filter(message => aiPromptType(message) === "澄清").length);
  const guidance = declaredOr("ai_guidance_count", messages.filter(message => aiPromptType(message) === "引导").length);
  return {clarification, guidance, total: declaredOr("ai_prompt_count", clarification + guidance)};
}

function processAlertMessages(row, labels) {
  let details = row.process_alert_details ?? row.process_alert_details_json;
  if (typeof details === "string") {
    try { details = JSON.parse(details); } catch { details = null; }
  }
  if (Array.isArray(details)) return details.map(detail => {
    if (typeof detail === "string") return detail;
    if (!detail || typeof detail !== "object") return "";
    const measured = Number.isFinite(detail.observed_value) && Number.isFinite(detail.threshold);
    if (detail.code === "pretest_long_identical_run" && measured) {
      return `连续选择同一位置较多：连续同一位置${detail.observed_value}题（阈值${detail.threshold}题）`;
    }
    return typeof detail.message === "string" ? detail.message : labels[detail.code] || "";
  }).filter(message => message.trim());
  return (Array.isArray(row.flags) ? row.flags : []).map(code => labels[code] || code);
}

function renderAiPromptSummary(data) {
  const box = $("ai-prompt-summary");
  if (!box) return;
  const scope = showcaseMode ? "全部记录" : "当前年龄";
  const totals = showcaseMode ? data.ai_prompt_totals_all_ages : data.ai_prompt_totals;
  const fields = ["ai_prompt_count", "ai_clarification_count", "ai_guidance_count", "records_with_ai_prompt", "record_count"];
  if (!totals || !fields.every(field => Number.isInteger(totals[field]) && totals[field] >= 0)) {
    box.replaceChildren(node("p", `${scope}暂无AI主动提示统计。`, "muted"));
    return;
  }
  const text = totals.record_count === 0
    ? `${scope}暂无可统计的已完成对话，AI主动提示共0次。`
    : `${scope}：${totals.records_with_ai_prompt}/${totals.record_count}条对话包含AI主动提示，共${totals.ai_prompt_count}次：澄清${totals.ai_clarification_count}次、引导${totals.ai_guidance_count}次。`;
  box.replaceChildren(node("p", text));
}

function renderShowcaseNotice() {
  const count = showcaseTotals?.sessions;
  const completed = showcaseTotals?.submitted;
  const unsubmitted = showcaseTotals?.unsubmitted;
  const box = $("admin-mode");
  const title = typeof count === "number" ? `大学生记录 · 共 ${count} 条` : "记录概况";
  box.replaceChildren(node("strong", title));
  if (typeof completed === "number" && typeof unsubmitted === "number") {
    box.append(node("span", `已完成 ${completed} 条 · 未完成 ${unsubmitted} 条`, "showcase-counts"));
  }
}

function configureAdminEnvironment(info) {
  showcaseMode = info.showcase_mode === true;
  showcaseTotals = info.showcase_totals || null;
  const comparison = $("showcase-comparison");
  if (comparison) comparison.hidden = !showcaseMode;
  if (!showcaseMode) return;
  renderShowcaseNotice();
  const replacements = {
    "admin-analysis-note": "任务按预设选项自动计分，当前方案的完成记录自动纳入统计。",
    "data-export-title": "导出记录",
    "data-export-description": "记录 CSV/JSON 保留全部记录，包括未完成记录和历史版本。常用列包含记录编号、年龄、分组、信息素养与任务评分、用时及 AI 主动提示、澄清和引导次数。已完成记录 CSV 仅包含当前方案中完成整份信息素养测试并已提交任务的记录，用于统计；当前同一批记录全部完成时，两类导出的人数可以相同。完整对话单独下载，各下载使用同一记录编号。导出范围覆盖全部年龄，不受上方筛选影响。",
    "analysis-export-link": "已完成记录 CSV"
  };
  for (const [id, text] of Object.entries(replacements)) if ($(id)) $(id).textContent = text;
}

function renderShowcaseComparison(comparison) {
  const box = $("showcase-comparison");
  if (!box) return;
  box.hidden = !showcaseMode;
  box.replaceChildren();
  if (!showcaseMode) return;
  const table = node("table", "", "item-table showcase-outcomes");
  table.append(node("caption", "全部记录的素养组比较"));
  const thead = node("thead"), head = node("tr");
  for (const text of ["AI信息素养", "无澄清人数", "无澄清均分", "有澄清人数", "有澄清均分", "均值差（分）"]) {
    const th = node("th", text); th.scope = "col"; head.append(th);
  }
  thead.append(head); table.append(thead);
  const body = node("tbody");
  const groups = Array.isArray(comparison?.groups) ? comparison.groups : [];
  for (const group of ["高", "中", "低"]) {
    const record = groups.find((item) => item.literacy_group === group) || {};
    const hasStandard = typeof record.standard_n === "number" && record.standard_n > 0;
    const hasAdaptive = typeof record.adaptive_n === "number" && record.adaptive_n > 0;
    const row = node("tr"), label = node("th", `${group}素养组`); label.scope = "row"; row.append(label);
    const values = [numberText(record.standard_n, 0), hasStandard ? numberText(record.standard_mean) : "—",
      numberText(record.adaptive_n, 0), hasAdaptive ? numberText(record.adaptive_mean) : "—",
      hasStandard && hasAdaptive ? numberText(record.difference_points) : "—"];
    for (const value of values) row.append(node("td", value));
    body.append(row);
  }
  table.append(body);
  const scroller = node("div", "", "table-scroll"); scroller.append(table); box.append(scroller);
  const gap = (value) => typeof value === "number" && Number.isFinite(value) ? `${numberText(value)}分` : "—";
  box.append(node("p", `高、低素养组均分差：无澄清 ${gap(comparison?.gap_standard)} → 有澄清 ${gap(comparison?.gap_adaptive)}。`, "showcase-gap"));
  box.append(node("p", "汇总18–25岁大学生方案的完成记录，不受下方年龄分层筛选影响。均值差＝有澄清－无澄清。", "muted"));
}

function renderConditionComparisons(box, data) {
  box.replaceChildren();
  const comparison = data.condition_comparison;
  if (!comparison?.comparisons?.length) {
    box.append(node("p", comparison ? "当前年龄层暂无完成样本的组间比较数据。" : "旧版接口未提供情境等权比较，请更新服务后查看。", "muted"));
    return;
  }
  const table = node("table", "", "item-table outcome-table");
  table.append(node("caption", "同年龄、同素养得分区间的决策质量比较（0–100分）"));
  const head = node("tr");
  for (const text of ["素养得分区间", "标准组记录数", "标准组均分", "澄清组记录数", "澄清组均分", "组间差（分）", "情境覆盖与提示"]) {
    const heading = node("th", text); heading.scope = "col"; head.append(heading);
  }
  const thead = node("thead"); thead.append(head); table.append(thead);
  const body = node("tbody");
  for (const record of comparison.comparisons) {
    const complete = record.complete !== false;
    const coverage = `${record.paired_scenarios ?? "—"}/${record.required_scenarios ?? "—"}个情境`;
    const notes = [coverage, ...(complete ? [] : ["有情境缺组，暂不汇总"]), ...(record.small_cell_warning ? ["小样本，差异尚不稳定"] : [])];
    const row = node("tr");
    const label = node("th", bandText(record.literacy_band)); label.scope = "row"; row.append(label);
    for (const value of [record.standard_n ?? 0, complete ? numberText(record.standard_mean) : "—", record.adaptive_n ?? 0,
      complete ? numberText(record.adaptive_mean) : "—", complete ? numberText(record.difference_points) : "—", notes.join("；")]) row.append(node("td", String(value)));
    body.append(row);
  }
  table.append(body);
  const scroller = node("div", "", "table-scroll"); scroller.append(table); box.append(scroller);
  box.append(node("p", "先比较每个情境的两组均分，再对各情境等权汇总。差值＝澄清组－标准组，是不同受试者的组间差，不能解释为同一人的前后提升。", "muted"));
  if (comparison.note) {
    const details = node("details"); details.append(node("summary", "统计口径"), node("p", comparison.note, "muted")); box.append(details);
  }
}

async function latestData(key, path, ageBand = null) {
  const version = (loadVersions.get(key) || 0) + 1;
  loadVersions.set(key, version);
  const current = () => loadVersions.get(key) === version
    && (ageBand === null || $("admin-age-band").value === ageBand);
  try {
    const data = await api(path);
    return current() ? data : null;
  } catch (error) {
    if (current()) throw error;
    return null;
  }
}

function actionButton(text, cls, key) {
  const button = node("button", text, cls);
  button.type = "button";
  button.dataset.actionKey = key;
  button.disabled = pendingActions.has(key);
  return button;
}

function syncActionButtons(key) {
  for (const button of document.querySelectorAll("[data-action-key]")) {
    if (button.dataset.actionKey === key) button.disabled = pendingActions.has(key);
  }
}

async function runAction(button, action, errorTarget = $("admin-error"), busyText = "保存中…") {
  const key = button.dataset.actionKey;
  if (button.disabled || pendingActions.has(key)) return;
  pendingActions.add(key);
  const label = button.textContent;
  button.textContent = busyText;
  syncActionButtons(key);
  errorTarget.textContent = "";
  try { await action(); }
  catch (error) { errorTarget.textContent = error.message; }
  finally {
    pendingActions.delete(key);
    button.textContent = label;
    syncActionButtons(key);
  }
}

async function refreshPanels(loaders, successText = "数据已更新。") {
  const version = ++feedbackVersion;
  $("admin-error").textContent = "";
  $("admin-status").textContent = "正在更新数据…";
  const results = await Promise.allSettled(loaders.map((load) => load()));
  if (version !== feedbackVersion) return;
  const errors = results.filter((result) => result.status === "rejected").map((result) => result.reason.message);
  $("admin-error").textContent = [...new Set(errors)].join("；");
  $("admin-status").textContent = errors.length ? "部分数据未更新，请重试。" : successText;
}

function distributionChart(targetId, points, labelFor) {
  const box = $(targetId); box.replaceChildren();
  const total = points.reduce((sum, point) => sum + point.S + point.A, 0);
  if (!total) { box.append(node("p", "暂无可展示的数据。", "muted")); return; }
  const maxCount = Math.max(1, ...points.flatMap((point) => [point.S, point.A]));
  const chart = node("div", "", "distribution-chart");
  chart.setAttribute("aria-hidden", "true");
  for (const point of points) {
    const column = node("div", "", "distribution-column");
    const pair = node("div", "", "distribution-pair");
    for (const group of ["S", "A"]) {
      const bar = node("div", "", group === "S" ? "distribution-bar standard" : "distribution-bar adaptive");
      bar.style.height = point[group] ? `${Math.max(5, Math.round(point[group] / maxCount * 120))}px` : "0";
      bar.title = `${labelFor(point)} · ${group === "S" ? "标准组" : "澄清组"} ${point[group]} 条`;
      pair.append(bar);
    }
    column.append(pair, node("small", labelFor(point)));
    chart.append(column);
  }
  const details = node("details");
  details.append(node("summary", "查看各区间记录数"));
  const table = node("table", "", "item-table distribution-table");
  table.append(node("caption", "各区间的标准组与澄清组记录数"));
  const head = node("tr");
  for (const title of ["区间", "标准组记录数", "澄清组记录数"]) {
    const th = node("th", title); th.scope = "col"; head.append(th);
  }
  const thead = node("thead"); thead.append(head); table.append(thead);
  const body = node("tbody");
  for (const point of points) {
    const row = node("tr");
    const heading = node("th", labelFor(point)); heading.scope = "row"; row.append(heading);
    row.append(node("td", String(point.S)), node("td", String(point.A))); body.append(row);
  }
  table.append(body);
  const scroller = node("div", "", "table-scroll"); scroller.append(table); details.append(scroller);
  box.append(chart, details);
}

async function loadSummary() {
  const ageBand = $("admin-age-band").value;
  const data = await latestData("summary", `/api/admin/summary?age_band=${encodeURIComponent(ageBand)}`, ageBand);
  if (!data) return;
  renderAiPromptSummary(data);
  renderShowcaseComparison(data.showcase_comparison);
  if (showcaseMode) {
    if (data.showcase_totals) showcaseTotals = data.showcase_totals;
    renderShowcaseNotice();
  } else $("admin-mode").textContent = data.study_mode === "demo"
    ? "任务测试。任务按预设选项自动计分。"
    : data.study_mode === "pilot"
      ? "研究者试跑。任务按预设选项自动计分。"
      : "实验任务。任务按预设选项自动计分。";
  const box = $("summary"); box.replaceChildren();
  box.append(node("p", data.active_task_title ? `当前任务：${data.active_task_title}` : "当前没有启用任务。", "wide muted"));
  for (const group of ["S", "A"]) {
    const record = data.groups.find((x) => x.group_id === group) || { sessions: 0, submitted: 0, analysis_n: 0, valid_mean_score: null };
    const analysisCount = record.analysis_n ?? record.valid ?? 0;
    const card = node("div", "", "mini-card");
    card.append(node("strong", group === "S" ? "标准组" : "澄清组"), node("p", `分配 ${record.sessions} · 完成 ${record.completed_n ?? record.submitted} · 纳入统计 ${analysisCount}`));
    card.append(node("small", record.valid_mean_score == null ? "暂无自动评分" : `完成样本平均分 ${Number(record.valid_mean_score).toFixed(1)}`));
    box.append(card);
  }
  box.append(node("p", data.mean_reply_seconds == null ? "暂无回复耗时记录" : `AI 平均回复耗时：${data.mean_reply_seconds.toFixed(1)} 秒`, "wide muted"));
  const strataBox = $("strata-summary"); strataBox.replaceChildren();
  for (let start = 0; start < 100; start += data.band_width) {
    const end = start + data.band_width;
    const band = `${String(start).padStart(3, "0")}-${String(end).padStart(3, "0")}`;
    const label = end === 100 ? `${start}–100%` : `${start}–<${end}%`;
    const standard = data.strata.find((x) => x.literacy_band === band && x.group_id === "S")?.sessions || 0;
    const adaptive = data.strata.find((x) => x.literacy_band === band && x.group_id === "A")?.sessions || 0;
    const card = node("div", "", "mini-card");
    card.append(node("strong", label), node("p", `标准组 ${standard} 条 · 澄清组 ${adaptive} 条`));
    strataBox.append(card);
  }
  renderConditionComparisons($("band-outcomes"), data);
  const literacy = Array.from({ length: 31 }, (_, raw) => ({ raw, S: 0, A: 0 }));
  for (const row of data.literacy_distribution) literacy[row.literacy_score][row.group_id] = row.sessions;
  distributionChart("literacy-distribution", literacy, (point) => String(point.raw));
  const scores = Array.from({ length: 5 }, (_, index) => ({ start: index * 20, S: 0, A: 0 }));
  for (const row of data.score_distribution) scores[row.start / 20][row.group_id] = row.sessions;
  distributionChart("score-distribution", scores, (point) => point.start === 80 ? "80–100" : `${point.start}–<${point.start + 20}`);
  const sourceBox = $("source-distribution"); sourceBox.replaceChildren();
  if (!data.evidence_reveal_distribution.length) sourceBox.append(node("p", "暂无对话线索记录。", "muted"));
  for (const sourceId of [...new Set(data.evidence_reveal_distribution.map((row) => row.card_id))].sort()) {
    const standard = data.evidence_reveal_distribution.find((row) => row.card_id === sourceId && row.group_id === "S")?.reveals || 0;
    const adaptive = data.evidence_reveal_distribution.find((row) => row.card_id === sourceId && row.group_id === "A")?.reveals || 0;
    const card = node("div", "", "mini-card");
    card.append(node("strong", `线索 ${sourceId}`), node("p", `标准组 ${standard} 次 · 澄清组 ${adaptive} 次`));
    sourceBox.append(card);
  }
}

async function loadPretestAnalysis() {
  const ageBand = $("admin-age-band").value;
  const data = await latestData("pretest", `/api/admin/pretest-analysis?age_band=${encodeURIComponent(ageBand)}`, ageBand);
  if (!data) return;
  const box = $("pretest-analysis"); box.replaceChildren();
  box.append(node("p", `信息素养测试 ${data.version} · ${data.item_count} 题 · 完整作答 ${data.n} 份。`));
  if (data.excluded_count) box.append(node("p", `另有${data.excluded_count}份作答因缺题、选项无效或版本不符未进入逐题统计。`, "muted"));
  if (!data.n) return;
  box.append(node("p", `平均得分 ${numberText(data.mean_percent)} · KR-20 ${numberText(data.kr20, 2)}。内部一致性不能单独证明这套大学生问卷的效度。`, "muted"));
  const table = node("table", "", "item-table");
  const head = node("tr");
  for (const title of ["题号", "维度", "答对比例（95%区间）", "校正题总相关", "高低组差值", "不确定作答", "题目与选项分析"]) head.append(node("th", title));
  table.append(head);
  for (const item of data.items || []) {
    const row = node("tr");
    const ci = item.p_correct_ci95;
    const interval = Array.isArray(ci) && ci.length === 2 ? `（${percentText(ci[0])}–${percentText(ci[1])}）` : "";
    const discrimination = numberText(item.high_low_discrimination, 2) + (item.discrimination_unstable ? "（不稳定）" : "");
    const uncertainty = `${item.uncertain_count ?? "—"}人${item.uncertain_rate == null ? "" : `（${percentText(item.uncertain_rate)}）`}`;
    for (const value of [item.id, item.domain, percentText(item.p_correct) + interval,
      numberText(item.corrected_item_total_r, 2), discrimination, uncertainty]) row.append(node("td", String(value)));
    const description = node("td"); description.append(node("p", item.question));
    if (item.discrimination_note) description.append(node("p", `高低组：${item.high_n ?? "—"}/${item.low_n ?? "—"}人。${item.discrimination_note}`, "muted"));
    if (Array.isArray(item.options) && item.options.length) {
      const details = node("details"); details.append(node("summary", "各选项选择情况"));
      const list = node("ul", "", "option-stat-list");
      for (const option of item.options) {
        const label = typeof option.index === "number" ? String.fromCharCode(65 + option.index) : "选项";
        list.append(node("li", `${label}. ${option.text}：${option.count ?? "—"}人（${percentText(option.selection_rate)}）；与其余题得分相关 ${numberText(option.selection_item_rest_r, 2)}${option.nonfunctioning_candidate ? "；选择率偏低，需人工复核" : ""}`));
      }
      details.append(list); description.append(details);
    }
    row.append(description);
    table.append(row);
  }
  const scroller = node("div", "", "table-scroll"); scroller.append(table); box.append(scroller);
  if (data.method_notes) {
    const details = node("details"); details.append(node("summary", "指标解释与限制"));
    for (const note of Object.values(data.method_notes)) if (typeof note === "string") details.append(node("p", note, "muted"));
    box.append(details);
  }
}

async function loadReviews() {
  const rows = await latestData("reviews", "/api/admin/review-queue");
  if (!rows) return;
  const submittedAt = (row) => beijingTimeText(row.ended_at) === "—" ? -Infinity : Date.parse(row.ended_at);
  reviewRows = rows.slice().sort((left, right) =>
    submittedAt(right) - submittedAt(left) || String(left.id).localeCompare(String(right.id)));
  reviewPage = Math.min(reviewPage, Math.max(0, Math.ceil(rows.length / REVIEW_PAGE_SIZE) - 1));
  renderReviews();
}

function reviewPagination() {
  const pages = Math.ceil(reviewRows.length / REVIEW_PAGE_SIZE);
  const controls = node("nav", "", "review-pagination");
  controls.setAttribute("aria-label", "记录分页");
  const first = reviewPage * REVIEW_PAGE_SIZE + 1;
  const last = Math.min((reviewPage + 1) * REVIEW_PAGE_SIZE, reviewRows.length);
  const label = node("span", `共 ${reviewRows.length} 条 · 第 ${reviewPage + 1}/${pages} 页 · 当前 ${first}–${last} 条`, "muted");
  label.setAttribute("aria-live", "polite");
  const previous = node("button", "上一页", "quiet"); previous.type = "button";
  const next = node("button", "下一页", "quiet"); next.type = "button";
  previous.disabled = reviewPage === 0; next.disabled = reviewPage >= pages - 1;
  const turnPage = (page) => {
    reviewPage = page; renderReviews();
    $("review-list").scrollIntoView({block: "start"});
    $("review-list").querySelector("nav button:not([disabled])")?.focus();
  };
  previous.addEventListener("click", () => turnPage(reviewPage - 1));
  next.addEventListener("click", () => turnPage(reviewPage + 1));
  controls.append(label, previous, next);
  return controls;
}

function renderReviews() {
  const rows = reviewRows.slice(reviewPage * REVIEW_PAGE_SIZE, (reviewPage + 1) * REVIEW_PAGE_SIZE);
  const box = $("review-list"); box.replaceChildren();
  if (!rows.length) { box.append(node("p", "暂无已提交的记录。", "muted")); return; }
  if (reviewRows.length > REVIEW_PAGE_SIZE) box.append(reviewPagination());
  const flagNames = {
    no_user_message: "没有提问", empty_submission: "未提交答案", very_short_dialogue: "对话极短",
    very_fast_completion: "完成过快", repeated_user_messages: "重复消息",
    low_character_diversity: "字符重复过多", low_keyword_overlap: "关键词重合少",
    no_evidence_retrieved: "没有取得资料线索", technical_interruption: "技术中断",
    model_fallback_used: "使用了模型回退回复",
    pretest_fast_completion: "信息素养测试用时偏短",
    pretest_long_identical_run: "连续选择同一位置较多"
  };
  for (const [index, row] of rows.entries()) {
    const card = node("article", "", "review-card");
    const recordNumber = typeof row.record_id === "string" && /^R\d{4,}$/.test(row.record_id)
      ? row.record_id : `R${String(reviewPage * REVIEW_PAGE_SIZE + index + 1).padStart(4, "0")}`;
    const title = node("h3", `记录 ${recordNumber} · ${row.ended_at ? "已完成" : "未完成"}`);
    const mode = { demo: "任务测试", pilot: "研究者试跑", live: "实验任务" }[row.run_mode] || (row.is_demo ? "任务测试" : "实验任务");
    card.append(title, node("p", `年龄区间 ${row.age_band || "旧版"} · 任务 ${row.task_id}${showcaseMode ? "" : ` · ${mode}`} · 提交 ${beijingTimeText(row.ended_at)}（北京时间）`, "muted"));
    const isShowcaseRecord = showcaseMode && typeof row.showcase_analysis_eligible === "boolean";
    const analysisEligible = isShowcaseRecord ? row.showcase_analysis_eligible
      : row.analysis_eligible ?? row.in_current_cohort;
    const cohortLabel = analysisEligible === true ? "可纳入当前统计"
      : analysisEligible === false ? "不计入当前统计" : "统计范围未标注";
    const reasons = isShowcaseRecord ? row.showcase_analysis_exclusion_reasons
      : row.analysis_exclusion_reasons ?? row.cohort_exclusion_reasons;
    const cohortReasons = Array.isArray(reasons) ? reasons.filter((reason) => typeof reason === "string" && reason)
      .map(reason => /合成|模拟/.test(reason) ? "记录来源不属于当前统计范围" : reason) : [];
    card.append(node("p", cohortLabel + (cohortReasons.length ? `：${cohortReasons.join("；")}` : ""), "muted"));
    const prompts = aiPromptCounts(row);
    card.append(node("p", `AI主动提示：${prompts.total}次（澄清${prompts.clarification}次、引导${prompts.guidance}次）`));
    const metadata = node("details"); metadata.append(node("summary", "查看时间、耗时与版本"));
    metadata.append(node("p", `记录编号：${recordNumber}`, "muted"));
    metadata.append(node("p", "以下时点均为北京时间；耗时不含问卷，包含等待和中途停留。", "muted"));
    for (const [label, value] of [["任务开始", row.created_at], ["进入决策", row.decision_started_at],
      ["最终提交", row.ended_at]]) {
      metadata.append(node("p", `${label}：${beijingTimeText(value)}`, "muted"));
    }
    metadata.append(node("p", `任务总耗时：${durationText(row.task_completion_seconds)} · 对话阶段：${durationText(row.dialogue_elapsed_seconds)} · 决策阶段：${durationText(row.decision_elapsed_seconds)}`, "muted"));
    for (const [label, value] of [["问卷版本", row.literacy_instrument_version], ["模型配置", row.model_config_id], ["提示词版本", row.prompt_version]]) {
      metadata.append(node("p", `${label}：${value || "—"}`, "muted"));
    }
    card.append(metadata);
    const alertMessages = processAlertMessages(row, flagNames);
    if (alertMessages.length) {
      const alerts = node("div", "", "flag-line");
      alerts.append(node("strong", "过程提醒"));
      const list = node("ul");
      for (const message of alertMessages) list.append(node("li", message));
      alerts.append(list); card.append(alerts);
    } else card.append(node("p", "未触发过程提醒", "muted"));
    const details = node("details"); details.append(node("summary", "查看完整对话与最终提交"));
    for (const m of row.messages || []) {
      const promptType = aiPromptType(m);
      const speaker = m.role === "user" ? "参与者" : promptType ? `AI（${promptType}）` : "AI";
      details.append(node("p", `${speaker}：${m.content}`, "transcript-line"));
    }
    if (!row.messages?.length) details.append(node("p", "本记录没有对话消息。", "muted"));
    if (row.decision_choice_text || row.decision_choice_id) details.append(node("p", `参加方案：${row.decision_choice_text || row.decision_choice_id}`, "transcript-line"));
    if (row.execution_choice_text || row.execution_choice_id) details.append(node("p", `办理方案：${row.execution_choice_text || row.execution_choice_id}`, "transcript-line"));
    details.append(node("p", typeof row.score === "number" && Number.isFinite(row.score) ? `自动评分：${numberText(row.score)} 分` : "暂无自动评分。", "transcript-line"));
    details.append(node("p", `补充说明：${row.submission || "（未填写）"}`, "transcript-line"));
    details.append(node("p", `统一参考资料：${row.reference_text}`, "transcript-line"));
    details.append(node("p", `预定评分标准：${row.rubric}`, "transcript-line"));
    card.append(details);
    box.append(card);
  }
  if (reviewRows.length > REVIEW_PAGE_SIZE) box.append(reviewPagination());
}

async function loadTasks() {
  const rows = await latestData("tasks", "/api/admin/tasks");
  if (!rows) return;
  const box = $("task-list"); box.replaceChildren(node("h3", "已有任务"));
  for (const task of rows) {
    if (showcaseMode && task.id === "demo-xinghe-grant" && !task.active) continue;
    const line = node("div", "", "task-row");
    line.append(node("span", `${task.age_band || "旧版"} · ${task.title}${showcaseMode ? "" : ` · ${task.is_demo ? "DEMO" : "LIVE"}`} · ${task.active ? "启用" : "停用"}`));
    const button = actionButton(task.active ? "停用" : "启用", "quiet", `task:${task.id}`);
    button.setAttribute("aria-label", `${task.active ? "停用" : "启用"}任务：${task.title}`);
    button.addEventListener("click", () => runAction(button, async () => {
      await api(`/api/admin/tasks/${task.id}/active/${task.active ? 0 : 1}`, { method: "POST" });
      await refreshPanels([loadTasks, loadSummary], "任务状态已更新。");
    }, $("admin-error"), "更新中…"));
    line.append(button); box.append(line);
  }
}

function jsonArrayField(data, name, label) {
  const value = String(data.get(name) || "").trim();
  if (!value) return [];
  let parsed;
  try { parsed = JSON.parse(value); } catch { throw new Error(`${label}不是有效的JSON，请检查引号、逗号和括号`); }
  if (!Array.isArray(parsed)) throw new Error(`${label}应填写JSON数组`);
  return parsed;
}

$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("task-error").textContent = "";
  const button = event.target.querySelector('button[type="submit"]');
  if (button.disabled) return;
  const data = new FormData(event.target);
  await runAction(button, async () => {
    const slots = String(data.get("slots") || "").split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const parts = line.split("|").map((x) => x.trim());
      if (parts.length < 3 || parts.length > 5 || !parts[0] || !parts[1] || !parts[2]) throw new Error("关键条件每行需填写key、说明、问题，可补匹配词与关联资料编号");
      return { key: parts[0], description: parts[1], question: parts[2], aliases: (parts[3] || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean), ...(parts[4] ? { evidence_card_id: parts[4] } : {}) };
    });
    const body = Object.fromEntries(["age_band", "title", "instructions", "shared_context", "participant_facts", "reference_text", "rubric"].map((key) => [key, String(data.get(key) || "")]));
    body.slots = slots; body.keywords = String(data.get("keywords") || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    body.evidence_cards = String(data.get("evidence_cards") || "").split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const parts = line.split("|").map((x) => x.trim());
      if (parts.length !== 4) throw new Error("对话线索每行需填写编号、标题、单条事实、关键词四项");
      return { id: parts[0], title: parts[1], body: parts[2], keywords: parts[3].split(/[,，]/).map((x) => x.trim()).filter(Boolean) };
    });
    body.decision_options = String(data.get("decision_options") || "").split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const parts = line.split("|").map((x) => x.trim());
      if (parts.length !== 3 || !parts[2] || !Number.isFinite(Number(parts[2]))) throw new Error("参加选项每行需填写编号、方案和数字分数");
      return { id: parts[0], text: parts[1], score: Number(parts[2]) };
    });
    body.execution_options = jsonArrayField(data, "execution_options", "办理选项");
    body.scenario_variants = jsonArrayField(data, "scenario_variants", "私事实情境");
    if (body.decision_options.length !== 5 || (body.execution_options.length && body.execution_options.length !== 5)) throw new Error("每组决策须恰好有5个选项");
    const maxScore = (options) => Math.max(...options.map((option) => {
      if (typeof option.score !== "number" || !Number.isFinite(option.score) || option.score < 0 || option.score > 100) throw new Error("每个选项的score须为0–100的数字");
      return option.score;
    }));
    const executionMax = body.execution_options.length ? maxScore(body.execution_options) : 0;
    if (maxScore(body.decision_options) + executionMax > 100) throw new Error("参加方案与办理方案的最高分相加不能超过100");
    for (const variant of body.scenario_variants) {
      if (!Array.isArray(variant.decision_options) || variant.decision_options.length !== 5) throw new Error("每个情境须含5个decision_options，编号和文字与基础参加选项一致");
      if (maxScore(variant.decision_options) + executionMax > 100) throw new Error("每个情境的参加方案与办理方案最高分相加不能超过100");
    }
    await api("/api/admin/tasks", { method: "POST", body: JSON.stringify(body) });
    event.target.reset(); await refreshPanels([loadTasks, loadSummary], "任务已创建。");
  }, $("task-error"), "创建中…");
});

async function refreshAll() {
  await runAction($("refresh-button"), () => refreshPanels([loadSummary, loadPretestAnalysis, loadReviews, loadTasks]), $("admin-error"), "刷新中…");
}
$("refresh-button").addEventListener("click", refreshAll);
$("admin-age-band").addEventListener("change", () => refreshPanels([loadSummary, loadPretestAnalysis]));
$("admin-logout-button")?.addEventListener("click", async () => {
  await runAction($("admin-logout-button"), async () => {
    await api("/api/admin/logout", { method: "POST" });
    window.location.replace("/admin/login");
  }, $("admin-error"), "退出中…");
});
async function initAdmin() {
  const info = await api("/api/info");
  configureAdminEnvironment(info);
  for (const band of info.age_bands || []) {
    for (const id of ["admin-age-band", "task-age-band"]) {
      const option = node("option", band.label); option.value = band.id; $(id).append(option);
    }
  }
  $("admin-age-band").value = (info.age_bands || []).find((band) => band.id === "18-20")?.id || info.age_bands?.[0]?.id || "";
  await refreshAll();
}
initAdmin().catch((error) => { $("admin-error").textContent = error.message; });
