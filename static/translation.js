"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const preview = document.body.dataset.researcherPreview === "true";
  const prefix = document.body.dataset.apiPrefix || "";
  const storageKey = `lens-translation-v1:${preview ? "preview" : "participant"}:${prefix}`;
  let saved = {}; try { saved = JSON.parse(sessionStorage.getItem(storageKey) || "{}"); } catch (_) { /* Fresh state after damaged browser storage. */ }
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) saved = {};
  const answerMap = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  let info = null, form = null, view = null, page = 0, busy = false, draftTimer = null, saveChain = Promise.resolve();
  let answers = answerMap(saved.answers), selfAnswers = answerMap(saved.selfAnswers), messageRequest = saved.messageRequest || null, startRequest = saved.startRequest || null;
  if (startRequest?.signature) { try { const signature = JSON.parse(startRequest.signature); delete signature.access_code; startRequest.signature = JSON.stringify(signature); } catch (_) { startRequest = null; } }
  let displayedMessageKey = "", displayedMaterialKey = "", countdownBase = null, starting = false, pollTimer = null, pollCount = 0;
  let movingPage = false, validationAttempted = false;
  const pageSize = 5;
  const eligibilityVersion = "cumt-undergraduate-v1";
  const ageBands = new Set(["18", "19", "20", "21", "22", "23-plus"]);
  const undergraduateYears = new Set(["year-1", "year-2", "year-3", "year-4", "year-5-plus"]);
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const writeSaved = () => {
    saved = {...saved, answers, selfAnswers, messageRequest, startRequest};
    try { sessionStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) { $("global-status").textContent = "浏览器暂存不可用，请在本页面完成任务。"; }
  };
  function textError(error) { return error instanceof TypeError ? "连接失败，请检查网络后重试。" : error.message || "操作失败，请重试。"; }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeoutMs = path.endsWith("/message") ? 210000 : options.method ? 30000 : 20000;
    let timeout;
    try {
      const deadline = new Promise((_, reject) => { timeout = setTimeout(() => { const error = new Error("请求等待超时。系统会保留请求编号，请同步会话或重试。"); error.code = "timeout"; reject(error); controller.abort(); }, timeoutMs); });
      const response = await Promise.race([fetch(prefix + path, {...options, signal: controller.signal, credentials: "same-origin", headers: {"Content-Type": "application/json", ...(options.headers || {})}}), deadline]);
      const body = await Promise.race([response.json().catch(() => ({})), deadline]);
      if (response.status === 401 && preview) { location.href = "/admin/login?next=" + encodeURIComponent("/admin/preview") + "&expired=1"; throw new Error("研究者登录已过期。"); }
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map(item => item.msg).join("；") : `请求失败（${response.status}）`;
        const error = new Error(detail); error.status = response.status; throw error;
      }
      return body;
    } finally { clearTimeout(timeout); }
  }
  const json = (method, value) => ({method, body: JSON.stringify(value)});
  function showPanel(name) {
    for (const id of ["intake-panel", "form-panel", "task-panel", "done-panel", "recovery-panel"]) $(id).hidden = id !== name;
    const step = name === "form-panel" || name === "intake-panel" ? 1 : name === "task-panel" ? 2 : 3;
    for (let i = 1; i <= 3; i++) { $("step-" + i).classList.toggle("active", i === step); $("step-" + i).classList.toggle("completed", i < step); }
    saved.stage = name === "form-panel" ? "form" : name === "intake-panel" ? "intake" : name;
    if (name === "form-panel") saved.formPage = page;
    writeSaved();
  }
  function intakeReady() {
    return Boolean(ageBands.has($("age-band").value) && undergraduateYears.has($("education-stage").value) && ["1", "2", "3", "4", "5"].includes($("translation-experience").value) && $("university-confirmed").checked && $("consent").checked && (preview || !info?.access_code_required || $("access-code").value.trim()) && !!form && (preview || info?.study_mode !== "live" || info.ready));
  }
  function updateIntake() {
    $("begin-form").disabled = !intakeReady();
    saved.intake = {age_band: $("age-band").value, education_stage: "bachelor", undergraduate_year: $("education-stage").value, eligibility_version: eligibilityVersion, translation_experience: $("translation-experience").value, university_confirmed: $("university-confirmed").checked, consent: $("consent").checked};
    writeSaved();
  }
  function questionItems() {
    return [...form.items.map(item => ({...item, self: false})), ...(form.self_items || []).map(item => ({...item, self: true}))];
  }
  const hasAnswer = item => {
    const value = (item.self ? selfAnswers : answers)[item.id];
    return Number.isInteger(value) && value >= 0 && value < item.options.length;
  };
  function currentItems() { return questionItems().slice(page * pageSize, (page + 1) * pageSize); }
  function missingItems() { return currentItems().filter(item => !hasAnswer(item)); }
  function missingCopy(missing) {
    const numbers = missing.map(item => questionItems().findIndex(candidate => candidate.id === item.id) + 1);
    return `本页还有 ${missing.length} 题未作答：第 ${numbers.join("、")} 题。请选择后再继续。`;
  }
  function syncVisibleAnswers() {
    // Native browser restoration may change checked states without a change event.
    // Read the actual visible choices before validation or leaving the page.
    const items = new Map(currentItems().map(item => [item.id, item]));
    let changed = false;
    for (const field of $("form-items").children) {
      const item = items.get(field.dataset.questionId); if (!item) continue;
      const selected = Array.from(field.querySelectorAll("input")).find(input => input.type === "radio" && input.name === `translation-${item.id}` && input.checked);
      const map = item.self ? selfAnswers : answers, value = selected ? Number(selected.value) : null;
      if (Number.isInteger(value) && value >= 0 && value < item.options.length) {
        if (map[item.id] !== value) { map[item.id] = value; changed = true; }
      } else if (Object.hasOwn(map, item.id)) { delete map[item.id]; changed = true; }
    }
    if (changed) startRequest = null;
    saved.formPage = page;
    writeSaved();
    return missingItems();
  }
  function updateValidationFeedback() {
    const missing = missingItems(), ids = new Set(missing.map(item => item.id));
    for (const field of $("form-items").children) {
      const invalid = validationAttempted && ids.has(field.dataset.questionId);
      field.classList.toggle("unanswered", invalid); field.setAttribute("aria-invalid", String(invalid));
      const message = field.querySelector(".question-error");
      if (message) { message.hidden = !invalid; message.textContent = invalid ? `第 ${field.dataset.questionNumber} 题尚未作答，请选择一个选项。` : ""; }
    }
    if (validationAttempted) $("form-error").textContent = missing.length ? missingCopy(missing) : "";
  }
  function renderForm() {
    const items = questionItems(), totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    page = Math.min(Math.max(0, Number.isInteger(page) ? page : 0), totalPages - 1);
    $("form-items").replaceChildren();
    currentItems().forEach((item, index) => {
      const number = page * pageSize + index + 1;
      const fieldset = document.createElement("fieldset"); fieldset.className = "question"; fieldset.dataset.questionId = item.id; fieldset.dataset.questionNumber = String(number);
      const legend = document.createElement("legend"); legend.textContent = `${number}. ${item.stem}`;
      const domain = document.createElement("span"); domain.className = "question-domain"; domain.textContent = item.self ? "实践自评 · 单独记录" : item.domain || "翻译判断"; legend.append(domain); fieldset.append(legend);
      const errorId = `question-${item.id}-error`;
      item.options.forEach((option, value) => {
        const label = document.createElement("label"); label.className = "question-option";
        const input = document.createElement("input"); input.type = "radio"; input.name = `translation-${item.id}`; input.value = String(value); input.checked = hasAnswer(item) && (item.self ? selfAnswers : answers)[item.id] === value;
        input.setAttribute("aria-describedby", errorId);
        const onChoice = () => { syncVisibleAnswers(); renderProgress(); updateValidationFeedback(); };
        input.addEventListener("input", onChoice); input.addEventListener("change", onChoice);
        const content = document.createElement("span"); content.textContent = typeof option === "string" ? option : option.text || option.label || String(option);
        label.append(input, content); fieldset.append(label);
      });
      const error = document.createElement("p"); error.id = errorId; error.className = "question-error"; error.hidden = true; fieldset.append(error);
      $("form-items").append(fieldset);
    });
    $("form-page").textContent = `第 ${page + 1} / ${totalPages} 页`; $("form-prev").disabled = page === 0 || starting; $("form-next").hidden = page === totalPages - 1; $("start-task").hidden = page !== totalPages - 1;
    saved.formPage = page; writeSaved(); renderProgress(); updateValidationFeedback();
  }
  function renderProgress() {
    const items = questionItems(), completed = items.filter(hasAnswer).length, local = currentItems();
    $("form-progress").textContent = `${completed} / ${items.length} 已作答`; $("form-meter").max = items.length; $("form-meter").value = completed;
    const answered = local.filter(hasAnswer).length;
    $("form-page-status").textContent = `本页已答 ${answered} / ${local.length} 题${answered === local.length ? "，可以继续" : ""}`;
  }
  function validatePage() {
    const missing = syncVisibleAnswers(); renderProgress(); validationAttempted = true; updateValidationFeedback();
    if (!missing.length) return true;
    const first = Array.from($("form-items").children).find(field => field.dataset.questionId === missing[0].id);
    first?.scrollIntoView({block: "center", behavior: "auto"}); first?.querySelector("input")?.focus({preventScroll: true});
    return false;
  }
  function movePage(direction) {
    if (movingPage || starting || (direction !== 1 && direction !== -1)) return false;
    movingPage = true;
    try {
      const last = Math.max(0, Math.ceil(questionItems().length / pageSize) - 1), target = page + direction;
      if (target < 0 || target > last) return false;
      if (direction === 1) { if (!validatePage()) return false; } else syncVisibleAnswers();
      page = target; validationAttempted = false; $("form-error").textContent = "";
      saved.stage = "form"; renderForm(); $("form-heading").focus();
      return true;
    } finally { movingPage = false; }
  }
  function restoreFormPage() {
    const items = questionItems(), firstMissing = items.findIndex(item => !hasAnswer(item));
    const last = Math.max(0, Math.ceil(items.length / pageSize) - 1);
    page = Number.isInteger(saved.formPage) ? Math.min(Math.max(saved.formPage, 0), last) : firstMissing < 0 ? last : Math.floor(firstMissing / pageSize);
    validationAttempted = false; showPanel("form-panel"); renderForm();
  }
  async function start() {
    if (starting || !validatePage()) return;
    const items = questionItems(), missingIndex = items.findIndex(item => !hasAnswer(item));
    if (missingIndex >= 0) { page = Math.floor(missingIndex / pageSize); validationAttempted = false; renderForm(); validatePage(); return; }
    if (!intakeReady()) {
      saved.formPage = page; showPanel("intake-panel"); $("global-status").textContent = "问卷答案已保留。请核对参加信息、勾选确认，并在需要时重新填写邀请码；继续后将返回当前问卷页。";
      $("intake-panel").scrollIntoView({block: "start"}); return;
    }
    starting = true; $("start-task").disabled = true; $("form-error").textContent = "";
    const payload = {consent: true, university_confirmed: true, age_band: $("age-band").value, education_stage: "bachelor", undergraduate_year: $("education-stage").value, translation_experience: Number($("translation-experience").value), pretest_version: form.version, answers, self_answers: selfAnswers, access_code: $("access-code").value, researcher_preview: preview};
    if (preview && $("preview-condition").value) payload.preview_condition = $("preview-condition").value;
    const signature = JSON.stringify({...payload, access_code: undefined});
    if (!startRequest || startRequest.signature !== signature) startRequest = {id: uuid(), signature};
    writeSaved(); $("global-status").textContent = "正在建立翻译会话…";
    try {
      const result = await api("/api/translation/start", json("POST", {...payload, request_id: startRequest.id}));
      saved.sessionId = result.id; saved.draft = result.draft || ""; saved.draftSessionId = result.id; saved.draftUpdatedAt = 0; messageRequest = null; writeSaved(); applyView(result, true); $("global-status").textContent = ""; $("task-title").scrollIntoView({block: "start"});
    } catch (error) { $("form-error").textContent = textError(error); $("global-status").textContent = ""; }
    finally { starting = false; $("start-task").disabled = false; }
  }
  function renderMaterials(task) {
    const key = `${view?.id}:${JSON.stringify(task)}`;
    if (key === displayedMaterialKey) return;
    displayedMaterialKey = key;
    $("task-title").textContent = task.title; $("task-instructions").textContent = task.instructions; $("source-text").textContent = task.source_text;
    $("task-brief").textContent = typeof task.brief === "string" ? task.brief : JSON.stringify(task.brief, null, 2); $("context-cards").replaceChildren();
    for (const card of task.context_cards || []) {
      const details = document.createElement("details"); details.className = "context-card"; const summary = document.createElement("summary"); summary.textContent = card.title;
      const body = document.createElement("p"); body.textContent = card.body; const copy = document.createElement("button"); copy.type = "button"; copy.className = "quiet"; copy.textContent = "复制到消息输入框";
      copy.addEventListener("click", () => { if (busy || view?.ended_at) return; $("message-input").value = [$("message-input").value.trim(), `${card.title}：${card.body}`].filter(Boolean).join("\n"); $("message-input").focus(); });
      details.append(summary, body, copy); $("context-cards").append(details);
    }
  }
  function renderMessages(messages) {
    const key = JSON.stringify(messages); if (key === displayedMessageKey) return; displayedMessageKey = key; $("chat-log").replaceChildren();
    if (!messages.length) { const empty = document.createElement("p"); empty.className = "chat-empty"; empty.textContent = "从你的翻译请求开始。你可以就原文、表达和译文修改与 AI 交流。"; $("chat-log").append(empty); }
    for (const message of messages) {
      const article = document.createElement("div"); article.className = `chat-message ${message.role === "user" ? "user" : "assistant"}`;
      const label = document.createElement("div"); label.className = "message-label"; label.textContent = message.role === "user" ? "你" : "AI 助手";
      const body = document.createElement("div"); body.className = "message-content"; body.textContent = message.content; article.append(label, body);
      if (message.role === "assistant") {
        const actions = document.createElement("div"); actions.className = "message-actions";
        const copy = document.createElement("button"); copy.type = "button"; copy.textContent = "复制回复"; copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(message.content); copy.textContent = "已复制"; } catch (_) { copy.textContent = "请选中文字后复制"; } });
        const use = document.createElement("button"); use.type = "button"; use.textContent = "放入译文工作区"; use.addEventListener("click", () => { if (view?.ended_at || busy) return; if ($("draft-input").value.trim() && !window.confirm("用这条 AI 回复替换工作区的草稿？请保留需要的译文部分。")) return; $("draft-input").value = message.content; draftChanged(); $("draft-input").focus(); });
        actions.append(copy, use); article.append(actions);
      } $("chat-log").append(article);
    }
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
  }
  function applyView(next, resetDraft = false) {
    const previousSessionId = saved.sessionId;
    const localDraftMatches = saved.draftSessionId === next.id || (!saved.draftSessionId && previousSessionId === next.id);
    const serverSavedAt = Date.parse(next.draft_saved_at || "") || 0;
    const useLocalDraft = localDraftMatches && typeof saved.draft === "string" && (saved.draftUpdatedAt || 0) > serverSavedAt;
    view = next; saved.sessionId = next.id;
    if (messageRequest && (next.completed_request_ids || []).includes(messageRequest.id)) {
      messageRequest = null; $("message-input").value = ""; $("chat-error").textContent = ""; $("chat-status").textContent = "回复已保存";
    }
    writeSaved();
    countdownBase = {seconds: Math.max(0, next.remaining_seconds || 0), time: Date.now()};
    if (next.ended_at) {
      showPanel("done-panel"); $("submitted-text").textContent = next.submission || next.draft || "";
      $("done-copy").textContent = next.study_mode === "demo" ? "本次为规则模拟演示，译文已保存，不生成研究质量评分。" : "译文已保存，等待研究者安排统一盲评。你的问卷和最终译文将分别用于研究分析。";
      $("new-session").hidden = !preview; $("global-status").textContent = ""; return;
    }
    showPanel("task-panel"); renderMaterials(next.task); renderMessages(next.messages || []);
    if (resetDraft) {
      if (!useLocalDraft && localDraftMatches && saved.draft && saved.draft !== (next.draft || "") && saved.draftUpdatedAt) saved.draftConflict = {sessionId: next.id, text: saved.draft};
      $("draft-input").value = useLocalDraft ? saved.draft : next.draft || "";
      $("draft-conflict").hidden = saved.draftConflict?.sessionId !== next.id;
      saved.draft = $("draft-input").value; saved.draftSessionId = next.id;
      if (!useLocalDraft) saved.draftUpdatedAt = serverSavedAt;
      writeSaved();
      if (useLocalDraft && $("draft-input").value !== (next.draft || "")) scheduleDraftSave();
    }
    $("turns").textContent = `${next.completed_turns || 0} / ${next.limits?.max_turns || info.max_turns} 轮`; updateControls(); updateClock(); draftCount();
    if (next.pending_reply) { $("chat-status").textContent = "本轮仍在生成，正在自动同步。"; schedulePoll(); }
    else { clearTimeout(pollTimer); pollTimer = null; pollCount = 0; }
  }
  function schedulePoll(delay = 2500) {
    if (pollTimer || !view || view.ended_at || !view.pending_reply) return;
    if (pollCount >= 120) { $("chat-status").textContent = "自动同步已暂停。请点击“同步会话”查看本轮状态。"; $("sync-button").hidden = false; return; }
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (!view || view.ended_at || !view.pending_reply) return;
      const id = view.id; pollCount++;
      try { const result = await api(`/api/translation/session/${encodeURIComponent(id)}`); if (view?.id === id) applyView(result); }
      catch (error) { if (view?.id === id) { $("chat-status").textContent = "自动同步暂未成功，可手动同步。"; $("sync-button").hidden = false; schedulePoll(10000); } }
    }, delay);
  }
  function updateControls() {
    if (!view) return;
    const blocked = busy || view.pending_reply || !!view.ended_at, expired = countdownBase && Math.max(0, countdownBase.seconds - (Date.now() - countdownBase.time) / 1000) <= 0;
    const maxTurns = view.limits?.max_turns || info.max_turns;
    $("send-button").disabled = blocked || expired || view.completed_turns >= maxTurns; $("message-input").disabled = blocked || expired || view.completed_turns >= maxTurns;
    $("draft-input").disabled = busy || !!view.ended_at; $("submit-button").disabled = blocked || !view.can_submit || $("draft-input").value.trim().length < 20;
    $("send-button").textContent = busy || view.pending_reply ? "等待回复…" : "发送 ↑";
    $("submit-hint").textContent = expired ? "对话时间已到。请整理译文并提交。" : view.completed_turns >= maxTurns ? "已达到对话轮次上限，请完成并提交译文。" : view.can_submit ? "请核对译文是否完整，提交后将被冻结。" : "完成至少一轮对话并填写完整译文后，即可提交。";
    $("sync-button").hidden = !view.pending_reply;
  }
  function updateClock() {
    if (!view || view.ended_at || !countdownBase) return;
    const seconds = Math.max(0, Math.ceil(countdownBase.seconds - (Date.now() - countdownBase.time) / 1000)); $("remaining").textContent = `剩余 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    if (!seconds) updateControls();
  }
  async function recover() {
    if (!saved.sessionId) return; $("global-status").textContent = "正在同步会话…"; $("global-error").textContent = "";
    try { const result = await api(`/api/translation/session/${encodeURIComponent(saved.sessionId)}`); applyView(result, true); $("global-status").textContent = ""; if (messageRequest) { $("message-input").value = messageRequest.content; $("chat-error").textContent = "上次请求未确认完成。可用相同内容重试，系统会避免重复记录。"; } }
    catch (error) { $("global-error").textContent = textError(error); $("global-status").textContent = ""; showPanel("recovery-panel"); }
  }
  async function send(event) {
    event.preventDefault(); const content = $("message-input").value.trim(); if (!content || busy || $("send-button").disabled) return;
    if (!messageRequest || messageRequest.content !== content) messageRequest = {id: uuid(), content}; writeSaved(); busy = true; updateControls(); $("chat-error").textContent = ""; $("chat-status").textContent = "AI 正在回复，请稍候…";
    try { const result = await api(`/api/translation/session/${encodeURIComponent(view.id)}/message`, json("POST", {content, request_id: messageRequest.id})); messageRequest = null; writeSaved(); $("message-input").value = ""; applyView(result); $("chat-status").textContent = "回复已保存"; }
    catch (error) {
      $("chat-error").textContent = textError(error) + " 可以重试本条消息。"; $("chat-status").textContent = ""; $("sync-button").hidden = false;
      try { const result = await api(`/api/translation/session/${encodeURIComponent(view.id)}`); applyView(result); } catch (_) { /* Keep the same request ID for a later safe retry. */ }
    }
    finally { busy = false; updateControls(); if ($("chat-error").textContent) $("sync-button").hidden = false; }
  }
  function draftCount() { $("draft-count").textContent = `${$("draft-input").value.length} 字符`; }
  function draftChanged() { delete saved.draftConflict; $("draft-conflict").hidden = true; saved.draft = $("draft-input").value; saved.draftSessionId = view?.id; saved.draftUpdatedAt = Date.now(); writeSaved(); draftCount(); updateControls(); $("draft-status").textContent = "正在暂存…"; scheduleDraftSave(); }
  function scheduleDraftSave() { clearTimeout(draftTimer); draftTimer = setTimeout(() => saveDraft(), 700); }
  function saveDraft() {
    clearTimeout(draftTimer); if (!view || view.ended_at) return saveChain;
    const id = view.id, draft = $("draft-input").value;
    saveChain = saveChain.catch(() => {}).then(async () => {
      if (!view || view.id !== id || view.ended_at) return;
      try { const result = await api(`/api/translation/session/${encodeURIComponent(id)}/draft`, json("PUT", {draft})); if (view?.id === id && $("draft-input").value === draft) { $("draft-status").textContent = "已自动保存"; saved.draft = draft; saved.draftSessionId = id; saved.draftUpdatedAt = Date.parse(result.draft_saved_at || "") || Date.now(); writeSaved(); } }
      catch (error) { $("draft-status").textContent = "草稿仅暂存在本标签页"; throw error; }
    });
    saveChain.catch(() => {}); return saveChain;
  }
  async function submit() {
    if (busy || $("submit-button").disabled) return;
    if (!window.confirm("确认提交当前工作区的最终译文？提交后不能继续修改或对话。")) return;
    busy = true; updateControls(); $("submit-error").textContent = ""; $("submit-button").textContent = "正在提交…";
    try {
      await saveDraft(); const payload = {submission: $("draft-input").value.trim()}; for (const id of ["burden", "helpfulness"]) if ($(id).value) payload[id] = Number($(id).value);
      const result = await api(`/api/translation/session/${encodeURIComponent(view.id)}/submit`, json("POST", payload)); applyView(result); $("done-panel").scrollIntoView({block: "start"});
    } catch (error) { $("submit-error").textContent = textError(error); }
    finally { busy = false; $("submit-button").textContent = "提交最终译文 →"; updateControls(); }
  }
  async function withdraw() {
    if (busy || !view || !window.confirm("退出并删除本次问卷、对话、草稿和译文？此操作无法恢复。")) return;
    busy = true; $("global-error").textContent = "";
    try { await saveChain.catch(() => {}); await api(`/api/translation/session/${encodeURIComponent(view.id)}`, {method: "DELETE"}); resetLocal(); $("global-status").textContent = "本次记录已删除。"; }
    catch (error) { $("global-error").textContent = textError(error); }
    finally { busy = false; updateControls(); }
  }
  function resetLocal() { clearTimeout(draftTimer); clearTimeout(pollTimer); pollTimer = null; pollCount = 0; saved = {}; view = null; answers = {}; selfAnswers = {}; page = 0; messageRequest = null; startRequest = null; displayedMessageKey = ""; writeSaved(); $("draft-input").value = ""; $("message-input").value = ""; $("consent").checked = false; $("university-confirmed").checked = false; updateIntake(); showPanel("intake-panel"); }
  $("begin-form").addEventListener("click", () => { if (intakeReady()) { updateIntake(); restoreFormPage(); $("global-status").textContent = ""; $("form-heading").focus(); } });
  for (const id of ["age-band", "education-stage", "translation-experience", "university-confirmed", "consent", "access-code"]) {
    $(id).addEventListener("input", updateIntake);
    $(id).addEventListener("change", updateIntake);
  }
  $("form-prev").addEventListener("click", () => movePage(-1));
  $("form-next").addEventListener("click", () => movePage(1));
  $("form-return-intake").addEventListener("click", () => { syncVisibleAnswers(); showPanel("intake-panel"); $("intake-panel").scrollIntoView({block: "start"}); });
  $("restore-local-draft").addEventListener("click", () => { if (saved.draftConflict?.sessionId === view?.id && !view?.ended_at) { $("draft-input").value = saved.draftConflict.text; draftChanged(); $("draft-input").focus(); } });
  $("start-task").addEventListener("click", start); $("chat-form").addEventListener("submit", send); $("draft-input").addEventListener("input", draftChanged); $("submit-button").addEventListener("click", submit);
  $("message-input").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("chat-form").requestSubmit(); } });
  $("withdraw-button").addEventListener("click", withdraw); $("done-withdraw").addEventListener("click", withdraw); $("recover-button").addEventListener("click", recover); $("sync-button").addEventListener("click", recover);
  $("discard-local").addEventListener("click", () => { if (window.confirm("只移除本标签页的会话连接？服务器记录仍会保留。若要删除记录，请先重新连接。")) { resetLocal(); $("global-error").textContent = ""; } });
  $("new-session").addEventListener("click", resetLocal); setInterval(updateClock, 1000);
  window.addEventListener("beforeunload", () => { if (saved.stage === "form" && form) syncVisibleAnswers(); if (view && !view.ended_at) { saved.draft = $("draft-input").value; saved.draftSessionId = view.id; writeSaved(); } });
  async function init() {
    try {
      [info, form] = await Promise.all([api("/api/translation/info"), api("/api/translation/form")]);
      if (saved.formVersion && saved.formVersion !== form.version) { answers = {}; selfAnswers = {}; startRequest = null; delete saved.formPage; saved.stage = "intake"; }
      saved.formVersion = form.version; writeSaved();
      if (saved.intake) {
        $("age-band").value = ageBands.has(saved.intake.age_band) ? saved.intake.age_band : "";
        $("education-stage").value = saved.intake.education_stage === "bachelor" && undergraduateYears.has(saved.intake.undergraduate_year) ? saved.intake.undergraduate_year : "";
        $("translation-experience").value = saved.intake.translation_experience || "";
        // Earlier intake covered all student stages. Do not infer an exact age,
        // undergraduate year or eligibility confirmation from that broader scope.
        $("university-confirmed").checked = saved.intake.eligibility_version === eligibilityVersion && saved.intake.education_stage === "bachelor" && saved.intake.university_confirmed === true;
        $("consent").checked = saved.intake.consent === true;
      }
      $("preview-controls").hidden = !preview; $("access-code-label").hidden = !info.access_code_required || preview;
      if (info.study_mode === "demo") { $("mode-banner").hidden = false; $("mode-banner").textContent = "演示模式 · AI 对话由内置规则模拟，不调用真实模型，也不生成译文质量评分。本次数据不能作为实证结果。"; }
      else if (info.study_mode === "pilot") { $("mode-banner").hidden = false; $("mode-banner").textContent = "预测试模式 · 对话将请求所配置的模型 API。使用候选研究材料，当前仅比较直接翻译与主动澄清。"; }
      else if (!info.ready) { $("mode-banner").hidden = false; $("mode-banner").classList.add("warning"); $("mode-banner").textContent = preview ? "正式实验材料尚未冻结。研究者仍可试跑，试跑记录不进入参与者统计。" : "正式实验材料尚未冻结，当前暂不开放新任务。请联系研究者。"; }
      else { $("mode-banner").hidden = false; $("mode-banner").textContent = "正式实验模式 · 当前材料已冻结，对话将请求所配置的模型 API。"; }
      if (info.study_mode !== "demo") { $("mode-banner").hidden = false; $("mode-banner").textContent += (info.model_api_verified ? " 本进程已记录成功 API 调用。" : " 本进程尚未记录成功 API 调用；配置存在不等于连通成功。"); }
      const resumeForm = saved.stage === "form" || (!saved.stage && !!saved.intake && questionItems().some(hasAnswer));
      $("global-status").textContent = ""; updateIntake();
      if (saved.sessionId) await recover();
      else if (resumeForm) { restoreFormPage(); $("global-status").textContent = `已恢复问卷第 ${page + 1} 页及已选答案；尚未创建研究会话。`; }
      else showPanel("intake-panel");
    } catch (error) { $("global-error").textContent = textError(error) + " 请刷新页面重试。"; $("global-status").textContent = ""; }
  }
  init();
})();
