const $ = (id) => document.getElementById(id);
const RESEARCHER_PREVIEW = document.body?.dataset?.researcherPreview === "true";
const previewApiPrefix = String(document.body?.dataset?.apiPrefix || "");
const API_PREFIX = RESEARCHER_PREVIEW && /^\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*\/?$/.test(previewApiPrefix)
  ? previewApiPrefix.replace(/\/$/, "") : "";
const PREVIEW_STORAGE_SUFFIX = API_PREFIX ? `:${encodeURIComponent(API_PREFIX)}` : "";
// university-v1 deliberately starts a fresh browser namespace for this study population.
const STORAGE_KEY = (RESEARCHER_PREVIEW ? "lens_university_v1_preview_session_id" : "lens_university_v1_session_id") + PREVIEW_STORAGE_SUFFIX;
const DRAFT_KEY = (RESEARCHER_PREVIEW ? "lens_university_v1_preview_pretest_draft_v1" : "lens_university_v1_pretest_draft_v1") + PREVIEW_STORAGE_SUFFIX;
const HISTORY_KEY = (RESEARCHER_PREVIEW ? "lens_university_v1_preview_session_history" : "lens_university_v1_session_history") + PREVIEW_STORAGE_SUFFIX;
const PENDING_MESSAGE_KEY = (RESEARCHER_PREVIEW ? "lens_university_v1_preview_pending_message" : "lens_university_v1_pending_message") + PREVIEW_STORAGE_SUFFIX;
const PENDING_START_KEY = (RESEARCHER_PREVIEW ? "lens_university_v1_preview_pending_start" : "lens_university_v1_pending_start") + PREVIEW_STORAGE_SUFFIX;
const UNIVERSITY_AGE_BANDS = new Set(["18-20", "21-23", "24-25"]);
const UNIVERSITY_FORM_VERSION = "student-literacy-v1-30";
const PAGE_SIZE = 6;
const volatileStorage = new Map();
function readStorage(storage, key) { try { return storage.getItem(key); } catch { return volatileStorage.get(key) || null; } }
function writeStorage(storage, key, value) {
  try { value == null ? storage.removeItem(key) : storage.setItem(key, value); } catch { /* Private browser storage may be unavailable. */ }
  if (value == null) volatileStorage.delete(key); else volatileStorage.set(key, value);
}
function readJSON(storage, key, fallback) { try { return JSON.parse(readStorage(storage, key)) || fallback; } catch { return fallback; } }
let sessionId = readStorage(localStorage, STORAGE_KEY);
let info = null;
let currentSession = null;
let compatibleSession = true;
let completedTurns = 0;
let inDecisionStage = false;
let maxTurns = 12;
let pendingReply = false;
let messageBusy = false;
let lifecycleBusy = false;
let starting = false;
let canEnterDecision = false;
let remainingSeconds = null;
let deadline = null;
let pretestItems = [];
let pretestVersion = "";
let pretestResponses = {};
let pretestPage = 0;
let pretestLoadSequence = 0;
let currentAgeBand = "";
let pollingTimer = null;
let draft = readJSON(sessionStorage, DRAFT_KEY, {});
let startRequestId = null;
let navigationSequence = 0;

async function api(path, options = {}) {
  let response;
  try { response = await fetch(`${API_PREFIX}${path}`, { headers: { "Content-Type": "application/json" }, ...options }); }
  catch { throw new Error("网络连接失败，请检查网络后重试"); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : `请求未完成（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return body;
}
function show(id) { $(id).hidden = false; }
function hide(id) { $(id).hidden = true; }
function setError(id, text = "") { $(id).textContent = text; }
function uuid() { return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`; }
function focusQuestionHeading() { $("pretest-heading").scrollIntoView({ behavior: "smooth", block: "start" }); $("pretest-heading").focus({ preventScroll: true }); }
function selectedDecision() { return document.querySelector('input[name="decision-choice"]:checked')?.value || null; }
function selectedExecution() { return document.querySelector('input[name="execution-choice"]:checked')?.value || null; }
function missingDecisionChoices() {
  const missing = [];
  if (document.querySelector('input[name="decision-choice"]') && !selectedDecision()) missing.push("参加方案");
  if (document.querySelector('input[name="execution-choice"]') && !selectedExecution()) missing.push("办理方案");
  if (!document.querySelector('input[name="decision-choice"]') && !$('submission-input').value.trim()) missing.push("任务结论");
  return missing;
}
function saveDraft() {
  if (!pretestVersion && draft.version && draft.ageBand === $("age-band").value) {
    draft = { ...draft, education: $("education-stage").value, familiarity: $("topic-familiarity").value };
    writeStorage(sessionStorage, DRAFT_KEY, JSON.stringify(draft)); return;
  }
  draft = { ageBand: $("age-band").value, version: pretestVersion, answers: pretestResponses, page: pretestPage,
    education: $("education-stage").value, familiarity: $("topic-familiarity").value };
  writeStorage(sessionStorage, DRAFT_KEY, JSON.stringify(draft));
}
function updateStartButton() {
  const completed = Object.keys(pretestResponses).length;
  $("pretest-progress").textContent = pretestItems.length ? `已完成 ${completed}/${pretestItems.length} 题` : "请选择年龄区间";
  $("pretest-meter").max = pretestItems.length || 30;
  $("pretest-meter").value = completed;
  const missing = [];
  if (!["college", "bachelor"].includes($("education-stage").value)) missing.push("在读学段");
  if (!$("student-confirmed").checked) missing.push("在读学生确认");
  if (!$("topic-familiarity").value) missing.push("熟悉程度");
  if (!$("consent-check").checked) missing.push("参与同意");
  if (info?.access_code_required && !$("access-code").value.trim()) missing.push("参与邀请码");
  if (completed !== pretestItems.length || !pretestItems.length) missing.unshift("全部判断题");
  $("start-button").disabled = starting || Boolean(sessionId) || !UNIVERSITY_AGE_BANDS.has(currentAgeBand) || missing.length > 0;
  $("start-hint").textContent = missing.length ? `还需完成：${missing.join("、")}。` : "准备完成，可以开始。";
}
function renderPretestPage() {
  const box = $("pretest-items"); box.replaceChildren();
  const start = pretestPage * PAGE_SIZE;
  let lastDomain = "";
  pretestItems.slice(start, start + PAGE_SIZE).forEach((item, index) => {
    if (item.domain !== lastDomain) {
      const heading = document.createElement("h4"); heading.className = "pretest-domain"; heading.textContent = item.domain;
      box.append(heading); lastDomain = item.domain;
    }
    const fieldset = document.createElement("fieldset"); fieldset.className = "pretest-item"; fieldset.id = `question-${item.id}`;
    const parts = item.question.split("\n").map((line) => line.trim()).filter(Boolean);
    const prompt = parts.pop() || item.question;
    const legend = document.createElement("legend"); legend.textContent = `${start + index + 1}. ${prompt}`;
    fieldset.append(legend);
    if (parts.length) {
      const material = document.createElement("p"); material.className = "pretest-material";
      material.id = `material-${item.id}`; material.textContent = parts.join("\n");
      fieldset.setAttribute("aria-describedby", material.id);
      fieldset.append(material);
    }
    item.options.forEach((option, choice) => {
      const label = document.createElement("label"); label.className = "pretest-option";
      const input = document.createElement("input"); input.type = "radio"; input.name = `pretest-${item.id}`; input.value = String(choice);
      input.checked = pretestResponses[item.id] === choice;
      input.addEventListener("change", () => {
        pretestResponses[item.id] = choice; startRequestId = null;
        fieldset.classList.remove("unanswered"); setError("pretest-error"); saveDraft(); updateStartButton();
      });
      const span = document.createElement("span"); span.textContent = option;
      label.append(input, span); fieldset.append(label);
    });
    box.append(fieldset);
  });
  const pages = Math.ceil(pretestItems.length / PAGE_SIZE);
  $("pretest-nav").hidden = !pretestItems.length;
  $("pretest-prev").disabled = pretestPage === 0 || starting;
  $("pretest-next").hidden = pretestPage >= pages - 1;
  $("pretest-page").textContent = `第 ${pretestPage + 1} / ${pages} 页`;
  $("start-controls").hidden = !pretestItems.length || pretestPage < pages - 1;
  updateStartButton();
}
function pageComplete() {
  const unanswered = pretestItems.slice(pretestPage * PAGE_SIZE, (pretestPage + 1) * PAGE_SIZE).filter((item) => pretestResponses[item.id] == null);
  if (!unanswered.length) return true;
  setError("pretest-error", "请完成本页题目；拿不准时可选“我暂时不会选”。");
  for (const item of unanswered) $(`question-${item.id}`).classList.add("unanswered");
  const first = $(`question-${unanswered[0].id}`);
  first.querySelector("input").focus(); first.scrollIntoView({ behavior: "smooth", block: "center" });
  return false;
}
async function loadPretest({ restore = false } = {}) {
  const sequence = ++pretestLoadSequence;
  const restoreDraft = structuredClone(draft);
  const band = info.age_bands.find((entry) => entry.id === $("age-band").value);
  currentAgeBand = band?.id || "";
  if (band && pretestVersion === UNIVERSITY_FORM_VERSION && pretestItems.length) {
    // All three university age bands share this instrument; keep the current answers and page.
    startRequestId = null; setError("start-error"); saveDraft(); updateStartButton(); return;
  }
  pretestItems = []; pretestResponses = {}; pretestVersion = ""; pretestPage = 0;
  $("pretest-items").replaceChildren(); hide("pretest-nav"); hide("start-controls"); show("pretest-loading");
  setError("pretest-error"); setError("start-error"); updateStartButton();
  if (!band) { $("pretest-loading").textContent = "选择年龄区间后载入问卷。"; return; }
  $("pretest-loading").textContent = "正在载入问卷…";
  try {
    const form = await api(`/api/pretest?age_band=${encodeURIComponent(band.id)}`);
    if (sequence !== pretestLoadSequence) return;
    if (form.version !== UNIVERSITY_FORM_VERSION) throw new Error("问卷版本与当前大学生方案不一致，请刷新页面后重试");
    pretestItems = form.items; pretestVersion = form.version;
    if (restore && restoreDraft.version === form.version) {
      for (const item of form.items) {
        const answer = restoreDraft.answers?.[item.id];
        if (Number.isInteger(answer) && answer >= 0 && answer < item.options.length) pretestResponses[item.id] = answer;
      }
      pretestPage = Math.max(0, Math.min(Math.ceil(form.items.length / PAGE_SIZE) - 1, Number(restoreDraft.page) || 0));
    }
    hide("pretest-loading"); renderPretestPage(); saveDraft();
  } catch (error) {
    if (sequence !== pretestLoadSequence) return;
    $("pretest-loading").textContent = "问卷暂未载入。请重新选择年龄区间重试，已保存的草稿会保留。";
    setError("start-error", error.message);
  }
}

function renderChoiceGroup(boxId, name, options, previous) {
  const box = $(boxId); box.replaceChildren();
  (options || []).forEach((option, index) => {
    const label = document.createElement("label"); label.className = "decision-option";
    const input = document.createElement("input"); input.type = "radio"; input.name = name; input.value = option.id; input.checked = option.id === previous;
    input.addEventListener("change", () => { setError("submit-error"); updateControls(); });
    const letter = document.createElement("strong"); letter.textContent = option.label || String.fromCharCode(65 + index);
    const text = document.createElement("span"); text.textContent = option.text;
    label.append(input, letter, text); box.append(label);
  });
}
function renderDecisionOptions(options, executionOptions = []) {
  const previous = selectedDecision(), previousExecution = selectedExecution();
  const dual = Boolean(executionOptions?.length);
  $("decision-heading").textContent = options?.length ? "提交你的决策" : "写下你的任务结论";
  $("decision-instructions").textContent = options?.length ? (dual ? "参加方案和办理方案各选一项。提交后不能修改。" : "选择最合适的一项。提交后不能修改。") : "请写出你的判断和依据。";
  $("decision-plan-group").hidden = !options?.length;
  $("execution-plan-group").hidden = !dual;
  $("decision-plan-heading").textContent = dual ? "1. 参加方案" : "行动方案";
  renderChoiceGroup("decision-options", "decision-choice", options, previous);
  renderChoiceGroup("execution-options", "execution-choice", executionOptions, previousExecution);
}
function addMessage(message) {
  const item = document.createElement("div"); item.className = `bubble ${message.role}`;
  const label = document.createElement("small"); label.textContent = message.role === "user" ? "你" : "AI";
  const text = document.createElement("p"); text.textContent = message.content;
  item.append(label, text); $("chat-log").append(item);
}
function updateControls() {
  const expired = remainingSeconds != null && remainingSeconds <= 0;
  const ended = Boolean(currentSession?.ended_at);
  const unavailable = !compatibleSession || ended;
  const waiting = messageBusy || pendingReply || lifecycleBusy;
  const exhausted = completedTurns >= maxTurns;
  $("turn-counter").textContent = `已完成 ${completedTurns}/${maxTurns} 轮`;
  $("chat-form").hidden = inDecisionStage || unavailable;
  $("decision-entry").hidden = inDecisionStage || ended;
  $("submit-card").hidden = !inDecisionStage || ended;
  $("send-button").disabled = unavailable || waiting || expired || exhausted;
  $("message-input").disabled = unavailable || waiting || expired || exhausted;
  $("send-button").textContent = messageBusy || pendingReply ? "回复中…" : "发送";
  $("chat-log").setAttribute("aria-busy", String(messageBusy || pendingReply));
  $("enter-decision-button").disabled = unavailable || waiting || !(canEnterDecision || expired);
  const missingChoices = missingDecisionChoices();
  $("submit-button").disabled = unavailable || waiting || !inDecisionStage || missingChoices.length > 0;
  $("decision-choices-hint").textContent = missingChoices.length ? `还需选择或填写：${missingChoices.join("、")}。` : "已选好，可以提交。";
  for (const id of ["withdraw-button", "withdraw-chat-button", "done-withdraw"]) $(id).disabled = lifecycleBusy;
  if (expired && !inDecisionStage) $("decision-entry-copy").textContent = "对话时间已到，请进入最终决策。";
  else if (exhausted && !inDecisionStage) $("decision-entry-copy").textContent = "已达到对话上限，请进入最终决策。";
  else $("decision-entry-copy").textContent = "完成两轮对话后可进入决策；进入后不能继续提问。";
  for (const field of $("submit-card").querySelectorAll("input,textarea,select")) field.disabled = lifecycleBusy;
}
function tickClock() {
  if (!deadline || !currentSession || currentSession.ended_at) { $("time-counter").textContent = ""; return; }
  if (inDecisionStage) { $("time-counter").textContent = "对话已结束"; return; }
  remainingSeconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
  $("time-counter").textContent = `对话剩余 ${Math.floor(remainingSeconds / 60)}:${String(remainingSeconds % 60).padStart(2, "0")}`;
  updateControls();
}
function matchesConfig(session) {
  return session.design_version === info.design_version && session.run_mode === info.study_mode
    && session.clarification_strategy === info.clarification_strategy && session.literacy_band_width === info.literacy_band_width
    && session.model_config_id === info.model_config_id && session.prompt_version.startsWith(info.answer_prompt_version);
}
function renderSession(data) {
  currentSession = data; compatibleSession = matchesConfig(data);
  const savedMessage = readJSON(sessionStorage, PENDING_MESSAGE_KEY, null);
  if (savedMessage?.sessionId === sessionId) {
    if (data.completed_request_ids?.includes(savedMessage.requestId)) {
      if ($("message-input").value.trim() === savedMessage.content || !$("message-input").value.trim()) $("message-input").value = "";
      writeStorage(sessionStorage, PENDING_MESSAGE_KEY, null);
    } else if (!$("message-input").value.trim()) $("message-input").value = savedMessage.content;
  }
  hide("consent-panel"); hide("resume-notice");
  $("task-title").textContent = data.task.title;
  $("task-instructions").textContent = data.task.instructions;
  $("task-context").textContent = data.task.shared_context;
  $("task-facts").textContent = data.task.participant_facts;
  renderDecisionOptions(data.task.decision_options, data.task.execution_options);
  $("chat-log").replaceChildren();
  for (const message of data.messages || []) addMessage(message);
  if (!data.messages?.length) { const empty = document.createElement("p"); empty.className = "chat-empty muted"; empty.textContent = "阅读案例后，在下方输入你想核查的问题。"; $("chat-log").append(empty); }
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
  completedTurns = data.completed_turns ?? (data.messages || []).filter((message) => message.role === "assistant").length;
  pendingReply = Boolean(data.pending_reply);
  inDecisionStage = Boolean(data.decision_stage);
  canEnterDecision = data.can_enter_decision ?? completedTurns >= 2;
  maxTurns = data.max_turns || info.max_turns;
  const seconds = data.remaining_seconds ?? (info.max_minutes * 60 - (Date.now() - Date.parse(data.created_at)) / 1000);
  remainingSeconds = Number.isFinite(seconds) ? Math.max(0, seconds) : null;
  deadline = remainingSeconds == null ? null : Date.now() + remainingSeconds * 1000;
  if (data.ended_at) {
    hide("experiment-panel"); show("done-panel");
    $("done-copy").textContent = "任务已保存并自动评分。";
    $("new-pilot-button").hidden = info.study_mode === "live" && !RESEARCHER_PREVIEW;
    $("new-pilot-button").textContent = "返回测试";
  } else {
    hide("done-panel"); show("experiment-panel");
    if (!compatibleSession) {
      show("resume-notice"); $("resume-copy").textContent = "实验版本已更新。本次记录已保留，可退出并删除，或开始新会话。";
      hide("resume-button"); show("restart-button");
    }
  }
  tickClock(); updateControls(); renderHistory();
  if (pendingReply) { $("chat-status").textContent = "AI 正在回复，页面会自动同步…"; schedulePoll(); }
  else { clearTimeout(pollingTimer); pollingTimer = null; }
}
async function syncSession() {
  if (!sessionId) return;
  const target = sessionId;
  const data = await api(`/api/session/${target}`);
  if (target === sessionId) renderSession(data);
  return data;
}
function showResumeFailure(error) {
  show("resume-notice"); show("resume-button"); hide("restart-button");
  $("resume-copy").textContent = `${error.message}。会话编号已保留，可重新连接。`;
  show("sync-button");
}
function schedulePoll() {
  clearTimeout(pollingTimer);
  const target = sessionId;
  pollingTimer = setTimeout(async () => {
    if (target !== sessionId) return;
    try { const data = await syncSession(); if (!data.pending_reply) { $("chat-status").textContent = "对话已同步。"; hide("sync-button"); } }
    catch (error) { setError("chat-error", `${error.message}。请点击“重新同步对话”。`); show("sync-button"); }
  }, 1800);
}
function archiveCurrent() {
  if (!sessionId) return;
  const history = readJSON(localStorage, HISTORY_KEY, []).filter((entry) => entry.id !== sessionId);
  history.unshift({ id: sessionId, label: currentSession?.task.title || "实验记录" });
  writeStorage(localStorage, HISTORY_KEY, JSON.stringify(history.slice(0, 20)));
}
function renderHistory() {
  const records = readJSON(localStorage, HISTORY_KEY, []).filter((entry) => entry.id !== sessionId);
  $("session-history").hidden = records.length === 0;
  const box = $("session-history-list"); box.replaceChildren();
  for (const record of records) {
    if (typeof record.id !== "string") continue;
    const line = document.createElement("div"); line.className = "history-row";
    const label = document.createElement("span"); label.textContent = `${record.label} · ${record.id.slice(0, 8)}`;
    const button = document.createElement("button"); button.type = "button"; button.className = "quiet"; button.textContent = "查看";
    button.addEventListener("click", async () => {
      if (messageBusy || pendingReply || lifecycleBusy || starting) return;
      const sequence = ++navigationSequence;
      const previous = sessionId;
      try {
        const recordData = await api(`/api/session/${record.id}`);
        if (sequence !== navigationSequence || messageBusy || pendingReply || lifecycleBusy || starting || sessionId !== previous) return;
        archiveCurrent(); sessionId = record.id; writeStorage(localStorage, STORAGE_KEY, sessionId); renderSession(recordData);
        window.scrollTo({ top: 0, behavior: "smooth" });
      } catch (error) {
        if (sequence !== navigationSequence) return;
        if (error.status === 404 || error.status === 410) {
          writeStorage(localStorage, HISTORY_KEY, JSON.stringify(records.filter((entry) => entry.id !== record.id))); renderHistory();
        } else showResumeFailure(error);
        sessionId = previous;
      }
    });
    line.append(label, button); box.append(line);
  }
}
async function beginNewSession() {
  if (messageBusy || pendingReply || lifecycleBusy) return;
  navigationSequence++; archiveCurrent(); sessionId = null; currentSession = null; writeStorage(localStorage, STORAGE_KEY, null);
  clearTimeout(pollingTimer); pollingTimer = null;
  writeStorage(sessionStorage, DRAFT_KEY, null); writeStorage(sessionStorage, PENDING_MESSAGE_KEY, null);
  writeStorage(sessionStorage, PENDING_START_KEY, null);
  draft = {}; startRequestId = null; pretestItems = []; pretestResponses = {}; pretestVersion = ""; pretestPage = 0;
  currentAgeBand = ""; completedTurns = 0; pendingReply = false; inDecisionStage = false; canEnterDecision = false;
  for (const control of $("consent-panel").querySelectorAll("input,select")) {
    if (control.type === "checkbox" || control.type === "radio") control.checked = false; else control.value = "";
  }
  for (const id of ["message-input", "submission-input", "burden-input", "helpfulness-input"]) $(id).value = "";
  for (const id of ["chat-error", "submit-error", "chat-status"]) setError(id);
  compatibleSession = true; deadline = null; remainingSeconds = null;
  hide("experiment-panel"); hide("done-panel"); hide("resume-notice"); hide("sync-button"); show("consent-panel");
  await loadPretest(); renderHistory(); window.scrollTo({ top: 0, behavior: "smooth" });
}

async function init() {
  try {
    info = await api(RESEARCHER_PREVIEW ? "/api/info?researcher_preview=true" : "/api/info"); maxTurns = info.max_turns;
    info.age_bands = (info.age_bands || []).filter((band) => UNIVERSITY_AGE_BANDS.has(band.id));
    $("access-code-label").hidden = !info.access_code_required;
    const education = $("education-stage");
    education.replaceChildren();
    const emptyEducation = document.createElement("option"); emptyEducation.value = ""; emptyEducation.textContent = "请选择"; education.append(emptyEducation);
    const stages = info.eligibility?.education_stages || [{id: "college", label: "在读专科"}, {id: "bachelor", label: "在读本科"}];
    for (const stage of stages.filter((entry) => ["college", "bachelor"].includes(entry.id))) {
      const option = document.createElement("option"); option.value = stage.id; option.textContent = stage.label; education.append(option);
    }
    for (const band of info.age_bands || []) {
      const option = document.createElement("option"); option.value = band.id; option.textContent = band.label; $("age-band").append(option);
    }
    show("mode-banner");
    $("mode-banner").textContent = RESEARCHER_PREVIEW
      ? "研究者试跑。"
      : info.study_mode === "live"
      ? "正式实验 · 请独立完成本次任务。" : "任务测试 · 先完成信息素养测试，再与 AI 查证资料。";
    if (info.age_bands.some((band) => band.id === draft.ageBand)) {
      $("age-band").value = draft.ageBand; $("education-stage").value = draft.education || ""; $("topic-familiarity").value = draft.familiarity || "";
      await loadPretest({ restore: true });
    }
    if (!info.has_active_task) setError("start-error", "当前没有可用任务，请联系研究者。");
    if (sessionId) {
      try { await syncSession(); }
      catch (error) {
        if (error.status === 404 || error.status === 410) { writeStorage(localStorage, STORAGE_KEY, null); sessionId = null; }
        else showResumeFailure(error);
      }
    }
    renderHistory(); updateStartButton();
  } catch (error) { setError("start-error", `${error.message}。请刷新页面重试。`); }
}

$("text-size-button").addEventListener("click", () => {
  const enabled = !document.documentElement.classList.contains("large-text");
  document.documentElement.classList.toggle("large-text", enabled);
  $("text-size-button").setAttribute("aria-pressed", String(enabled));
  writeStorage(localStorage, "lens_large_text", enabled ? "1" : "0");
});
if (readStorage(localStorage, "lens_large_text") === "1") { document.documentElement.classList.add("large-text"); $("text-size-button").setAttribute("aria-pressed", "true"); }
$("pretest-prev").addEventListener("click", () => { if (pretestPage > 0) { pretestPage--; renderPretestPage(); saveDraft(); setError("pretest-error"); focusQuestionHeading(); } });
$("pretest-next").addEventListener("click", () => { if (pageComplete()) { pretestPage++; renderPretestPage(); saveDraft(); focusQuestionHeading(); } });
for (const id of ["consent-check", "student-confirmed", "education-stage", "topic-familiarity", "access-code"]) {
  $(id).addEventListener(id === "access-code" ? "input" : "change", () => { startRequestId = null; updateStartButton(); if (["education-stage", "topic-familiarity"].includes(id)) saveDraft(); });
}
$("age-band").addEventListener("change", async () => {
  const restore = draft.version === UNIVERSITY_FORM_VERSION;
  startRequestId = null; await loadPretest({ restore });
});
$("start-button").addEventListener("click", async () => {
  if (starting || $("start-button").disabled) return;
  starting = true; navigationSequence++; startRequestId ||= uuid(); setError("start-error");
  for (const control of $("consent-panel").querySelectorAll("input,select,button")) control.disabled = true;
  try {
    const payload = { researcher_preview: RESEARCHER_PREVIEW, consent: $("consent-check").checked,
      age_band: currentAgeBand, education_stage: $("education-stage").value, student_confirmed: $("student-confirmed").checked,
      topic_familiarity: Number($("topic-familiarity").value),
      pretest_version: pretestVersion, pretest_answers: Object.fromEntries(pretestItems.map((item) => [item.id, pretestResponses[item.id]])), access_code: $("access-code").value.trim() };
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(payload)));
    const payloadHash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    const pendingStart = readJSON(sessionStorage, PENDING_START_KEY, null);
    if (pendingStart?.payloadHash === payloadHash) startRequestId = pendingStart.requestId;
    writeStorage(sessionStorage, PENDING_START_KEY, JSON.stringify({ payloadHash, requestId: startRequestId }));
    const started = await api("/api/start", { method: "POST", body: JSON.stringify({ ...payload, request_id: startRequestId }) });
    navigationSequence++;
    sessionId = started.session_id; writeStorage(localStorage, STORAGE_KEY, sessionId);
    writeStorage(sessionStorage, PENDING_START_KEY, null);
    writeStorage(sessionStorage, DRAFT_KEY, null); draft = {}; startRequestId = null;
    await syncSession(); $("experiment-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { if (sessionId) showResumeFailure(error); else setError("start-error", error.message); }
  finally {
    starting = false;
    for (const control of $("consent-panel").querySelectorAll("input,select,button")) control.disabled = false;
    if (pretestItems.length) renderPretestPage(); else updateStartButton();
  }
});
let messageComposing = false;
$("message-input").addEventListener("compositionstart", () => { messageComposing = true; });
$("message-input").addEventListener("compositionend", () => { messageComposing = false; });
$("message-input").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || messageComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (event.repeat || $("message-input").disabled || $("send-button").disabled || !$("message-input").value.trim()) return;
  $("chat-form").requestSubmit();
});
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = $("message-input").value.trim();
  if (!content || !sessionId || $("send-button").disabled) return;
  const target = sessionId;
  let request = readJSON(sessionStorage, PENDING_MESSAGE_KEY, null);
  if (!request || request.sessionId !== target || request.content !== content) request = { sessionId: target, content, requestId: uuid() };
  writeStorage(sessionStorage, PENDING_MESSAGE_KEY, JSON.stringify(request));
  messageBusy = true; setError("chat-error"); hide("sync-button"); $("chat-status").textContent = "AI 正在回复…"; updateControls();
  const begin = performance.now();
  try {
    await api(`/api/session/${target}/message`, { method: "POST", body: JSON.stringify({ content, request_id: request.requestId }) });
    if (target !== sessionId) return;
    $("message-input").value = ""; writeStorage(sessionStorage, PENDING_MESSAGE_KEY, null);
    await syncSession(); $("chat-status").textContent = `已回复 · ${((performance.now() - begin) / 1000).toFixed(1)} 秒`;
  } catch (error) {
    if (target !== sessionId) return;
    setError("chat-error", `${error.message}。输入已保留，可重试或同步。`); show("sync-button");
    try { await syncSession(); } catch { /* Keep the session token and draft during network failures. */ }
  } finally { if (target === sessionId) { messageBusy = false; updateControls(); } }
});
$("enter-decision-button").addEventListener("click", async () => {
  if (!sessionId || $("enter-decision-button").disabled || !confirm("进入最终决策后不能继续提问。确认进入？")) return;
  lifecycleBusy = true; updateControls(); setError("chat-error");
  try { await api(`/api/session/${sessionId}/decision`, { method: "POST" }); await syncSession(); $("submit-card").scrollIntoView({ behavior: "smooth", block: "start" }); }
  catch (error) { setError("chat-error", error.message); try { await syncSession(); } catch { show("sync-button"); } }
  finally { lifecycleBusy = false; updateControls(); }
});
$("submission-input").addEventListener("input", updateControls);
$("submit-button").addEventListener("click", async () => {
  if (!sessionId || $("submit-button").disabled) return;
  lifecycleBusy = true; updateControls(); setError("submit-error");
  try {
    await api(`/api/session/${sessionId}/submit`, { method: "POST", body: JSON.stringify({ decision_choice_id: selectedDecision(), execution_choice_id: selectedExecution(),
      submission: $("submission-input").value, burden: $("burden-input").value ? Number($("burden-input").value) : null,
      helpfulness: $("helpfulness-input").value ? Number($("helpfulness-input").value) : null }) });
    await syncSession(); $("done-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { setError("submit-error", error.message); try { await syncSession(); } catch { showResumeFailure(error); } }
  finally { lifecycleBusy = false; updateControls(); }
});
async function withdraw() {
  if (!sessionId || lifecycleBusy || !confirm("删除本次对话和提交内容？此操作无法撤销。")) return;
  const target = sessionId; lifecycleBusy = true; updateControls();
  try {
    await api(`/api/session/${target}`, { method: "DELETE" });
    navigationSequence++;
    writeStorage(localStorage, HISTORY_KEY, JSON.stringify(readJSON(localStorage, HISTORY_KEY, []).filter((entry) => entry.id !== target)));
    writeStorage(localStorage, STORAGE_KEY, null); writeStorage(sessionStorage, PENDING_MESSAGE_KEY, null);
    sessionId = null; location.reload();
  } catch (error) { alert(error.message); lifecycleBusy = false; updateControls(); }
}
for (const id of ["withdraw-button", "withdraw-chat-button", "done-withdraw"]) $(id).addEventListener("click", withdraw);
for (const id of ["resume-button", "sync-button"]) $(id).addEventListener("click", async () => { try { await syncSession(); setError("chat-error"); hide("sync-button"); } catch (error) { showResumeFailure(error); } });
$("restart-button").addEventListener("click", beginNewSession);
$("new-pilot-button").addEventListener("click", beginNewSession);
setInterval(tickClock, 1000);
init();
