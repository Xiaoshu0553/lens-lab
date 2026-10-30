"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const bandNames = {low: "基础能力组", medium: "中等能力组", high: "较高能力组"};
  const conditions = {S: "S · 直接翻译", A: "A · 主动澄清"};
  const conditionName = value => conditions[value] || (value === "O" ? "O · 历史优化试跑（不纳入当前测试）" : value || "—");
  const dimensionNames = {accuracy: "语义准确", context: "语境与人物一致", fluency: "中文流畅", style: "文体与声音", completeness: "完整与克制"};
  const dimensionName = key => materials?.rubric?.find(item => item.id === key)?.label || dimensionNames[key] || key;
  const statuses = {pending: "待统一评审", completed: "已评审", demo_unscored: "未评分", evaluating: "评审中", failed: "评审失败，可重试"};
  let summary = null, records = [], materials = null, currentId = null, page = 0, refreshing = false;
  let modelConfiguration = null, judgeEditors = [], modelsBusy = false, modelsDirty = false, dialogueDirty = false;
  let batchBusy = false, batchStopRequested = false;
  const evaluationMode = value => value?.evaluation_mode || "distinct_models";
  const repeatedEvaluation = value => evaluationMode(value) === "repeated_sessions";
  const number = (value, digits = 1) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
  const integer = value => Number.isFinite(value) ? String(value) : "0";
  const signed = value => typeof value === "number" && Number.isFinite(value) ? `${value > 0 ? "+" : ""}${value.toFixed(1)}` : "—";
  const time = value => { if (!value) return "—"; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(parsed); };
  const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined && text !== null) node.textContent = text; if (className) node.className = className; return node; };
  const json = (method, body) => ({method, body: JSON.stringify(body)});
  const auditJSON = value => JSON.stringify(value, (key, item) => ["study_mode", "run_mode", "runtime_mode"].includes(key) ? undefined : item, 2);
  async function api(path, options = {}) {
    const controller = new AbortController(), timeoutMs = path.endsWith("/evaluate") || path.endsWith("/models/check") ? 135000 : 30000;
    let timeout;
    try {
      const deadline = new Promise((_, reject) => { timeout = setTimeout(() => { const error = new Error("请求等待超时。已接受的评分会继续处理，请刷新记录查看状态后再重试。"); error.code = "timeout"; reject(error); controller.abort(); }, timeoutMs); });
      const response = await Promise.race([fetch(path, {...options, signal: controller.signal, credentials: "same-origin", headers: {"Content-Type": "application/json", ...(options.headers || {})}}), deadline]);
      const body = await Promise.race([response.json().catch(() => ({})), deadline]);
      if (response.status === 401) { location.href = "/admin/login?next=" + encodeURIComponent(location.pathname === "/admin/instruments" ? "/admin/instruments" : "/admin") + "&expired=1"; const error = new Error("登录已过期。"); error.status = 401; throw error; }
      if (!response.ok) { const error = new Error(typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map(item => item.msg).join("；") : `请求失败（${response.status}）`); error.status = response.status; throw error; }
      return body;
    } finally { clearTimeout(timeout); }
  }
  const errorText = error => error instanceof TypeError ? "连接失败，请检查网络后重试。" : error.message;
  function table(headers, rows, options = {}) {
    if (!rows.length) return el("div", options.empty || "暂时没有记录。", "empty-state");
    const wrap = el("div", null, "table-scroll"), tableNode = el("table", null, "research-table"), head = el("thead"), header = el("tr");
    for (const text of headers) { const th = el("th", text); th.scope = "col"; header.append(th); } head.append(header); tableNode.append(head);
    const body = el("tbody");
    for (const cells of rows) { const row = el("tr"); for (const content of cells) { const td = el("td"); if (content instanceof Node) td.append(content); else td.textContent = content === null || content === undefined ? "—" : String(content); row.append(td); } body.append(row); }
    tableNode.append(body); wrap.append(tableNode); return wrap;
  }
  function renderSummary() {
    const counts = summary.counts || {}; $("counts").replaceChildren();
    for (const [key, label, hint] of [["total", "翻译会话数", "当前版本"], ["submitted", "已提交译文", "首次提交冻结"], ["scored", "已完成评审", "当前评审批次完整评分"], ["needs_review", "待研究者复核", "分歧或严重错误"], ["previews", "研究者试跑", "不进入组间统计"]]) {
      const card = el("div", null, "stat"); card.append(el("span", label, "label"), el("span", integer(counts[key]), "value"), el("span", hint, "hint")); $("counts").append(card);
    }
    const rows = [];
    for (const band of ["low", "medium", "high"]) for (const condition of ["S", "A"]) {
      const cell = (summary.cells || []).find(item => item.band === band && item.condition === condition) || {};
      rows.push([bandNames[band], conditions[condition], integer(cell.n), integer(cell.submitted), integer(cell.scored), number(cell.mean_score), number(cell.mean_turns), typeof cell.mean_seconds === "number" ? `${number(cell.mean_seconds / 60)} 分钟` : "—"]);
    }
    $("cells").replaceChildren(table(["问卷能力组", "交互条件", "会话数", "已提交译文", "已评分", "译文均分 / 100", "平均对话轮数", "平均翻译会话时长"], rows));
    $("gaps").replaceChildren(table(["交互条件", "高 − 低 / 分"], ["S", "A"].map(condition => [conditions[condition], signed((summary.gaps || []).find(item => item.condition === condition)?.high_low_gap)])));
    $("effects").replaceChildren(table(["能力组", "主动澄清收益 A − S"], ["low", "medium", "high"].map(band => { const row = (summary.effects || []).find(item => item.band === band) || {}; return [bandNames[band], signed(row.clarification_gain)]; })));
    $("hypothesis-note").textContent = summary.hypothesis_note || "当前检验主动澄清收益是否随翻译能力不同而变化，结果未预设。针对性优化模块须在本阶段结果分析后另行设计。";
    $("admin-mode").hidden = false;
    $("admin-mode").textContent = "当前只比较直接翻译与主动澄清。汇总当前配置的翻译会话及当前评审批次的完整评分。";
    $("admin-mode").textContent += summary.model_api_verified ? " 本进程已记录成功 API 调用" + (summary.model_api_verified_at ? "（" + time(summary.model_api_verified_at) + "）" : "") + "。" : summary.model_api_verified_at ? " 此前已连通模型 API；最近一次调用未成功，请稍后重试。" : " 本进程尚未记录成功 API 调用。";
    $("admin-mode").textContent += summary.panel_ready ? repeatedEvaluation(summary) ? ` 同模型多会话重复评审配置完整（${summary.evaluation_session_count || summary.model_count || 0}个独立上下文会话）；会话使用同一模型，存在共同偏差。` : ` 评审团配置完整（${summary.model_count || 0}个不同模型）；各模型连通状态见“模型与 API”。` : " 评审配置尚未完整，不能生成综合质量分。请打开“模型与 API”配置评审方式。";
    $("batch-evaluate-button").disabled = batchBusy || !summary.panel_ready || !summary.evaluation_batch_id;
    renderEvaluationProtocol(evaluationMode(summary));
  }
  function filteredRecords() {
    return records.filter(record => ($("cohort-filter").value === "all" || $("cohort-filter").value === "current" && record.current_cohort !== false || $("cohort-filter").value === "historical" && record.current_cohort === false) && ($("kind-filter").value === "all" || record.record_kind === $("kind-filter").value) && ($("condition-filter").value === "all" || record.condition === $("condition-filter").value) && ($("assessment-filter").value === "all" || $("assessment-filter").value === "review" && record.needs_review || record.assessment_status === $("assessment-filter").value));
  }
  function renderRecords() {
    const filtered = filteredRecords(), pages = Math.max(1, Math.ceil(filtered.length / 15)); page = Math.min(page, pages - 1); $("record-count").textContent = `${filtered.length} 条记录`;
    $("record-list").replaceChildren(table(["记录编号", "开始时间", "配置版本", "类型", "能力组 / 得分", "条件", "对话轮数", "评审状态", "质量分", "操作"], filtered.slice(page * 15, page * 15 + 15).map(record => {
      const id = el("span", record.id.slice(0, 8)); id.title = record.id;
      const state = el("span", record.needs_review ? "需复核" : record.ended_at ? statuses[record.assessment_status] || record.assessment_status : "任务进行中", record.needs_review ? "tag warning" : "tag");
      const button = el("button", "查看记录", "quiet"); button.addEventListener("click", () => openRecord(record.id));
      return [id, time(record.created_at), record.current_cohort === false ? "历史配置" : "当前配置", record.record_kind === "researcher_preview" ? "研究者试跑" : "翻译会话", `${bandNames[record.band] || record.band || "—"} / ${number(record.ability_score, 0)}`, conditionName(record.condition), integer(record.turns), state, number(record.score), button];
    }), {empty: records.length ? "没有符合筛选条件的记录。" : "还没有翻译研究记录。可通过研究者试跑检查完整流程。"}));
    $("record-pagination").hidden = filtered.length <= 15; $("records-prev").disabled = page === 0; $("records-next").disabled = page === pages - 1; $("records-page").textContent = `${page + 1} / ${pages}`;
  }
  function definition(pairs) { const dl = el("dl", null, "detail-meta"); for (const [key, value] of pairs) { const group = el("div"); group.append(el("dt", key), el("dd", value)); dl.append(group); } return dl; }
  function assessmentForBatch(record, batch) { return [record.active_assessment, record.assessment, ...(record.assessment_history || [])].find(assessment => assessment && assessment.evaluation_batch_id === batch); }
  function assessmentView(assessment) {
    const box = el("div");
    if (!assessment || assessment.status === "pending" || assessment.status === "demo_unscored") { box.append(el("p", assessment?.status === "demo_unscored" ? "该记录没有质量评分。" : "尚未完成统一评审。", "muted")); return box; }
    const result = assessment.result || assessment;
    if (result.score === undefined || result.score === null) { box.append(el("p", statuses[assessment.status] || "等待评分", "muted")); return box; }
    const multiModel = !!(result.model_totals || result.trimmed_model_ids || result.ratings?.some(rating => rating.model_id));
    const repeated = repeatedEvaluation(result);
    box.append(el("h3", repeated ? "同模型多会话重复评审结果" : multiModel ? "多模型共同量规盲评结果" : "历史同模型角色评分（不属于当前多模型评审）"));
    const line = el("p"); line.append(el("span", number(result.score) + " / 100", "score-big"), el("span", result.needs_review ? "  · 触发复核" : "  · 未触发预设复核阈值", result.needs_review ? "tag warning" : "muted small")); box.append(line);
    const trimmed = result.trimmed_model_ids || result.trimmed_expert_ids || [];
    box.append(definition([["总分标准差", number(result.sd)], ["总分极差", number(result.range)], [repeated ? "去除最高 / 最低会话" : multiModel ? "去除最高 / 最低模型" : "历史去除角色", trimmed.join(" / ")]]));
    if (result.aggregation) box.append(el("p", result.aggregation, "admin-section-note"));
    if (result.reason) box.append(el("p", result.reason, "translation-assessment-note"));
    if (result.dimensions) box.append(table(["维度", repeated ? "保留会话加权均分" : "同一模型集合加权均分"], Object.entries(result.dimensions).map(([key, value]) => [dimensionName(key), number(value)])));
    for (const rating of result.ratings || []) {
      const details = el("details", null, "materials-item"), id = rating.model_id || rating.expert_id;
      const label = rating.label || (multiModel ? id : materials?.experts?.find(item => item.id === id)?.label || id);
      const total = rating.total ?? result.model_totals?.[id] ?? result.expert_totals?.[id];
      const isTrimmed = rating.trimmed === true || trimmed.includes(id);
      details.append(el("summary", `${label || "评审模型"}${rating.model ? " · " + rating.model : ""}${isTrimmed ? " · 已去除极值" : ""} · 总分 ${number(total)}`));
      if (multiModel) details.append(definition([[repeated ? "评审会话编号" : "模型标识", id || "—"], [repeated ? "预设会话权重" : "预设模型权重", number(rating.weight ?? result.model_weights?.[id], 3)], ["是否纳入综合分", isTrimmed ? "否：去除极值" : "是"]]));
      details.append(table(["评分维度", "分数"], Object.entries(rating.scores || {}).map(([key, value]) => [dimensionName(key), number(value)])));
      const list = el("ul"); for (const item of rating.evidence || []) list.append(el("li", item)); details.append(list);
      if (rating.critical_errors?.length) details.append(el("p", "严重错误标记：" + rating.critical_errors.join("；"), "error")); box.append(details);
    }
    box.append(el("p", result.limitation || (repeated ? "同一模型的独立上下文会话可反映重复评分波动，仍共享模型偏差；去极值平均不能提供不同模型的一致性证据，须结合人工盲评校准。" : "不同模型的偏差可能相关；多模型去极值加权不能保证无偏，须结合人工盲评校准。"), "admin-section-note"));
    return box;
  }
  async function submissionHash(text) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, "0")).join("");
  }
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  async function activeEvaluationBatch(fixed = null) {
    const current = await api("/api/admin/translation/summary");
    if (!current.panel_ready || !current.evaluation_batch_id || !current.cohort_id) throw new Error("请先保存完整评审配置并检查 API。");
    if (fixed && (current.cohort_id !== fixed.cohort_id || current.evaluation_batch_id !== fixed.evaluation_batch_id || evaluationMode(current) !== evaluationMode(fixed))) throw new Error("评审批次或版本已变化，已停止后续评审；此前结果保留。");
    return current;
  }
  async function settledEvaluation(id, hash, fixed, expectedKind = "participant") {
    const deadline = Date.now() + 650000;
    while (true) {
      await activeEvaluationBatch(fixed);
      const loaded = await api(`/api/admin/translation/records/${encodeURIComponent(id)}`), record = loaded.record || loaded;
      if (record.record_kind !== expectedKind || !record.ended_at || record.current_cohort === false || record.cohort_id !== fixed.cohort_id || typeof record.submission !== "string" || await submissionHash(record.submission) !== hash) throw new Error("记录范围或已提交译文发生变化，已停止评审。");
      if (!record.evaluation_pending) return record;
      if (Date.now() >= deadline) throw new Error("已接受的评审仍在处理，已停止后续请求；请稍后同步记录。");
      await delay(2500);
    }
  }
  async function evaluateCurrentRecord(id, hash, fixed, allowReassessment = false, expectedKind = "participant") {
    for (let attempt = 0; attempt < 3; attempt++) {
      const record = await settledEvaluation(id, hash, fixed, expectedKind);
      const stored = assessmentForBatch(record, fixed.evaluation_batch_id);
      if (stored) return stored;
      if (record.assessment && !allowReassessment) throw new Error("此译文已有其他批次评分；请明确选择新批次重评，原评分将保留。");
      try {
        const result = await api(`/api/admin/translation/records/${encodeURIComponent(id)}/evaluate`, json("POST", {evaluation_batch_id: fixed.evaluation_batch_id, submission_sha256: hash, allow_reassessment: allowReassessment}));
        if (result.evaluation_batch_id !== fixed.evaluation_batch_id) throw new Error("返回评分所属批次已变化，已停止后续评审。");
        return result;
      } catch (error) {
        if (error.status !== undefined && ![409, 502].includes(error.status)) throw error;
        if (error.status === undefined && error.code !== "timeout" && !(error instanceof TypeError)) throw error;
        const recovered = await settledEvaluation(id, hash, fixed, expectedKind);
        const stored = assessmentForBatch(recovered, fixed.evaluation_batch_id);
        if (stored) return stored;
        if (error.status === 409 || attempt === 2) throw error;
        await delay(1000 * (attempt + 1));
      }
    }
  }
  async function openRecord(id, scroll = true) {
    $("record-detail").hidden = false; $("record-title").textContent = "正在读取…"; $("detail-content").replaceChildren(); $("detail-error").textContent = ""; currentId = id;
    try {
      const loaded = await api(`/api/admin/translation/records/${encodeURIComponent(id)}`); if (currentId !== id) return;
      const record = loaded.record || loaded; $("record-title").textContent = `记录 ${record.id || id}`; const box = $("detail-content");
      if (record.current_cohort === false) box.append(el("p", "历史版本或其他模型配置记录：不进入当前汇总，保留其原始问卷、对话与评分。", "notice warning"));
      box.append(definition([["记录类型", record.record_kind === "researcher_preview" ? "研究者试跑" : "翻译会话"], ["交互条件", conditionName(record.condition)], ["能力组 / 客观题得分", `${bandNames[record.band] || record.band || "—"} / ${number(record.ability_score, 0)}`], ["翻译会话开始", time(record.created_at)], ["译文提交时间", time(record.ended_at)], ["翻译会话时长", typeof record.duration_seconds === "number" ? `${number(record.duration_seconds / 60)} 分钟` : "—"]]));
      box.append(el("h3", "最终译文"), el("div", record.submission || "尚未提交", "record-submission"));
      const assessment = assessmentForBatch(record, summary?.evaluation_batch_id) || loaded.assessment || record.assessment;
      box.append(assessmentView(assessment));
      const priorAssessments = [record.assessment, ...(record.assessment_history || [])].filter(previous => previous && previous !== assessment && previous.evaluation_batch_id !== assessment?.evaluation_batch_id);
      if (priorAssessments.length) {
        const history = el("details", null, "materials-item"); history.append(el("summary", `历史评审批次 · ${priorAssessments.length}`));
        for (const previous of priorAssessments) { history.append(el("p", `批次 ${previous.evaluation_batch_id || "历史批次"}`, "muted small"), assessmentView(previous)); }
        box.append(history);
      }
      const reassessment = !!(assessment && summary?.evaluation_batch_id && assessment.evaluation_batch_id !== summary.evaluation_batch_id);
      if (record.ended_at && record.current_cohort !== false && summary?.study_mode !== "demo" && ((record.assessment_status || assessment?.status) !== "completed" || reassessment)) {
        const repeated = repeatedEvaluation(summary), label = reassessment ? "用当前批次重新评审（保留原结果）" : repeated ? "运行同模型多会话重复评审" : "运行多模型统一盲评";
        const actions = el("div", null, "detail-actions"), button = el("button", label, "primary"), hint = el("span", repeated ? "同一模型的每个新会话均评价全部五维，完整评分后统一入库。" : "不同模型均评价全部五维；全部模型评分完整后统一入库。", "muted small");
        button.disabled = batchBusy || summary?.panel_ready === false;
        if (button.disabled) hint.textContent = repeated ? "请先保存5—9次同模型独立上下文评审配置。" : "评审团配置不足，须先在“模型与 API”中配置至少5个不同模型。";
        button.addEventListener("click", async () => {
          button.disabled = true; button.textContent = "正在完成统一评审…"; $("detail-error").textContent = "";
          try { const fixed = await activeEvaluationBatch(); await evaluateCurrentRecord(id, await submissionHash(record.submission), fixed, reassessment, record.record_kind || "participant"); await refresh(); await openRecord(id, false); }
          catch (error) { $("detail-error").textContent = errorText(error); button.disabled = false; button.textContent = "重试统一盲评"; }
        }); actions.append(button, hint); box.append(actions);
      }
      const dialogue = el("details", null, "materials-item"); dialogue.open = true; dialogue.append(el("summary", "完整对话"));
      for (const message of loaded.messages || record.messages || []) { const line = el("div", null, `chat-message ${message.role === "user" ? "user" : "assistant"}`); line.append(el("div", `${message.role === "user" ? "参与者" : "AI 助手"} · ${time(message.created_at)}${message.action ? " · " + message.action : ""}`, "message-label"), el("div", message.content, "message-content")); dialogue.append(line); } box.append(dialogue);
      const replyEvents = (loaded.events || record.events || []).filter(event => event.type === "reply");
      if (replyEvents.length) {
        const timing = el("details", null, "materials-item"); timing.append(el("summary", "逐轮回复耗时"));
        timing.append(table(["回复轮次", "总耗时 / 秒"], replyEvents.map((event, index) => [index + 1, number(event.seconds, 2)])));
        timing.append(el("p", "用时与质量差值均按实际记录描述，不能单独证明公平性。当前测试未启用优化模块；历史试跑的完整过程仍保留在下方审计记录。", "admin-section-note")); box.append(timing);
      }
      const audit = el("details", null, "materials-item"); audit.append(el("summary", "版本、问卷与过程审计"), el("pre", auditJSON(loaded), "json-view")); box.append(audit);
      if (scroll) $("record-detail").scrollIntoView({block: "start"});
    } catch (error) { $("record-title").textContent = "记录读取失败"; $("detail-error").textContent = errorText(error); }
  }
  function materialDetails(title, text, open = false) { const details = el("details", null, "materials-item"); details.open = open; details.append(el("summary", title), el("p", text)); return details; }
  async function showMaterials() {
    $("materials-panel").hidden = false; $("materials-status").textContent = "正在读取材料…"; $("materials-content").replaceChildren(); $("materials-panel").scrollIntoView({block: "start"});
    try {
      materials = await api("/api/admin/translation/materials");
      $("materials-status").textContent = `${materials.material_status || "候选研究材料，待审查与验证"} · ${materials.frozen ? "当前版本已冻结" + (materials.frozen_at ? "（" + time(materials.frozen_at) + "）" : "") : "当前版本未冻结"}`;
      const box = $("materials-content");
      box.append(materialDetails("当前版本与模型配置", auditJSON({versions: materials.versions, model_config: materials.model_config, cohort_id: materials.cohort_id})));
      box.append(materialDetails("两组交互策略", JSON.stringify({policies: materials.policies}, null, 2)));
      const task = materials.task || {}; box.append(materialDetails(task.title || "共同翻译任务", [task.instructions, "原文：\n" + task.source_text, "翻译委托：\n" + (typeof task.brief === "string" ? task.brief : JSON.stringify(task.brief))].filter(Boolean).join("\n\n"), true));
      const context = el("details", null, "materials-item"); context.append(el("summary", "补充语境、参考译文与关键评分点"));
      for (const card of task.context_cards || []) context.append(el("h4", card.title), el("p", card.body));
      context.append(el("h4", "参考译文（允许成立的不同表达）"), el("p", task.reference_translation || "—"), el("h4", "关键评分点"), el("pre", JSON.stringify(task.critical_points || [], null, 2), "json-view")); box.append(context);
      const questions = el("details", null, "materials-item"); questions.append(el("summary", "客观能力问卷与答案依据"));
      for (const [index, item] of (materials.form?.items || materials.form_items || []).entries()) { const field = el("div", null, "materials-item"); field.append(el("strong", `${index + 1}. ${item.stem}`)); const options = el("ol"); for (const [value, text] of (item.options || []).entries()) options.append(el("li", typeof text === "string" ? text : text.text, item.correct === value ? "correct" : undefined)); field.append(options); if (item.rationale) field.append(el("p", "依据：" + item.rationale, "muted")); questions.append(field); } box.append(questions);
      const self = el("details", null, "materials-item"); self.append(el("summary", "实践自评（不计入客观能力分组）")); for (const item of materials.form?.self_items || materials.self_items || []) { self.append(el("p", item.stem)); const options = el("ol"); for (const option of item.options || []) options.append(el("li", option)); self.append(options); } box.append(self);
      box.append(table(["评分维度", "权重", "说明"], (materials.rubric || []).map(item => [item.label, `${item.weight}%`, item.description || item.criteria || "按统一量规评分"])));
      const repeated = repeatedEvaluation(materials) || repeatedEvaluation(summary);
      box.append(table([repeated ? "评审会话" : "评审模型", "模型 ID", "预设权重"], (materials.models || materials.judges || materials.panel?.judges || []).map(item => [item.label || item.id, item.model, number(item.weight, 3)])));
      box.append(el("p", repeated ? "同模型多会话重复评审：每个全新上下文会话均评价全部五维，不读取其他评分。模型与共同偏差相同，逐次结果和当前批次配置保留。" : "每个模型使用相同的完整五维评分要求，互不读取其他模型评分。评审模型与权重在研究配置中冻结。", "admin-section-note"));
      $("materials-freeze").hidden = materials.frozen || summary?.study_mode !== "live";
    } catch (error) { $("materials-status").textContent = errorText(error); }
  }
  function connectionLabel(model) {
    if (!model?.model) return "尚未配置模型";
    return `${model.model} · ${model.verified_at ? "最近成功调用：" + time(model.verified_at) : "尚无成功调用记录"}${model.last_error ? " · " + model.last_error : ""}`;
  }
  function renderEvaluationProtocol(mode) {
    const repeated = mode === "repeated_sessions";
    $("evaluation-protocol-heading").textContent = repeated ? "同模型多会话重复评审，使用共同五维量规" : "不同模型共同评审，使用共同五维量规";
    $("evaluation-protocol-description").textContent = repeated ? "每份译文建立多个全新上下文会话，使用同一模型和相同量规。每个会话均评价全部五个维度，不读取翻译对话、能力组、条件或其他会话的评分。去掉一个最高和一个最低会话总分后，汇总其余会话。逐次评分、依据、权重和分歧均保留。" : "每个不同模型使用相同原文、完整语境和共同五维量规，不读取翻译对话、能力组、条件或其他模型的评分。先计算每个模型总分，去掉一个最高和一个最低总分，再按预设权重汇总。逐模型评分、依据、权重和分歧均保留。";
    $("evaluation-limitation").textContent = repeated ? "同一模型的多次评分共享系统性偏差。独立上下文会话可记录重复评分的波动，不能代表多个不同 AI，也不能保证偏差抵消或无偏。高分歧与严重错误须人工复核。" : "不同模型的偏差可能相关，去极值与加权平均不能保证无偏。高分歧与严重错误须人工复核，并通过人工盲评校准。完整评审不足时不发布部分结果的综合分。";
  }
  function renderEvaluationMode() {
    const repeated = $("evaluation-mode").value === "repeated_sessions";
    $("evaluation-repetitions-label").hidden = !repeated;
    $("evaluation-repetitions").disabled = modelsBusy || batchBusy || !repeated;
    $("distinct-judges-panel").hidden = repeated;
    $("evaluation-mode-note").textContent = repeated ? "同模型多会话重复评审：复用已保存的翻译对话模型 API，逐份译文建立5—9个全新上下文会话，每个会话评价全部五维。已有不同模型连接会保留。共同模型偏差仍然存在。" : "不同模型共同评审：须保存5—9个不同模型连接，每个模型评价全部五维。模型权重应在查看评分前确定。";
  }
  function inputField(label, value, options = {}) {
    const wrapper = el("label", label), input = el("input");
    input.type = options.type || "text"; input.value = value ?? ""; input.autocomplete = input.type === "password" ? "new-password" : "off";
    if (options.placeholder) input.placeholder = options.placeholder;
    if (options.min !== undefined) input.min = options.min;
    if (options.step !== undefined) input.step = options.step;
    input.addEventListener("input", () => { modelsDirty = true; });
    wrapper.append(input); return {wrapper, input};
  }
  function judgePayloads() {
    return judgeEditors.map(editor => ({id: editor.id.value.trim(), label: editor.label.value.trim(), model: editor.model.value.trim(), base_url: editor.base_url.value.trim(), weight: Number(editor.weight.value), api_key: editor.api_key.value})).filter(model => model.model || model.base_url);
  }
  function renderJudges(models, transientKeys = false) {
    $("judge-models").replaceChildren(); judgeEditors = [];
    for (const [index, model] of models.entries()) {
      const card = el("article", null, "materials-item"), header = el("div", null, "section-heading"), fields = el("div", null, "intake-grid");
      header.append(el("h4", `评审模型 ${index + 1}`)); const remove = el("button", "移除此模型", "quiet"); remove.type = "button";
      remove.addEventListener("click", () => { const draft = judgeEditors.map(editor => ({id: editor.id.value, label: editor.label.value, model: editor.model.value, base_url: editor.base_url.value, weight: Number(editor.weight.value), api_key: editor.api_key.value})); draft.splice(index, 1); modelsDirty = true; renderJudges(draft, true); });
      header.append(remove); card.append(header);
      const editor = {};
      for (const [key, label, options] of [["id", "配置标识", {placeholder: "例如 judge-1"}], ["label", "显示名称", {placeholder: "例如 模型一"}], ["model", "实际模型名称 / ID", {placeholder: "服务实际支持的模型名称"}], ["base_url", "API 基础地址", {type: "url", placeholder: "https://服务地址/v1"}], ["weight", "预设模型权重", {type: "number", min: "0.001", step: "0.1"}], ["api_key", "API 密钥", {type: "password", placeholder: model.api_key_configured ? "已配置；留空保留原密钥" : "本地无认证服务可留空"}]]) {
        const field = inputField(label, key === "api_key" ? transientKeys ? model.api_key || "" : "" : model[key] ?? (key === "weight" ? 1 : key === "id" ? `judge-${index + 1}` : ""), options); editor[key] = field.input; fields.append(field.wrapper);
      }
      editor.remove = remove; judgeEditors.push(editor); card.append(fields, el("p", connectionLabel(model), "admin-section-note")); $("judge-models").append(card);
    }
    $("add-judge-model").disabled = modelsBusy || judgeEditors.length >= (modelConfiguration?.panel?.maximum || 9);
  }
  function renderModelConfiguration(config) {
    modelConfiguration = config;
    const dialogue = config.dialogue || {};
    $("dialogue-model").value = dialogue.model || ""; $("dialogue-base-url").value = dialogue.base_url || "";
    $("dialogue-api-key").value = ""; $("dialogue-api-key").placeholder = dialogue.api_key_configured ? "已配置；留空保留原密钥" : "本地无认证服务可留空";
    $("dialogue-temperature").value = dialogue.temperature ?? 0; $("dialogue-max-tokens").value = dialogue.max_tokens ?? 1600;
    $("dialogue-connection").textContent = connectionLabel(dialogue);
    $("evaluation-mode").value = evaluationMode(config);
    $("evaluation-repetitions").value = config.evaluation_repetitions ?? 5;
    const judges = [...(config.configured_judges || (repeatedEvaluation(config) ? [] : config.judges) || [])]; while (judges.length < (config.panel?.minimum || 5)) judges.push({id: `judge-${judges.length + 1}`, weight: 1});
    renderJudges(judges); modelsDirty = false; dialogueDirty = false; renderEvaluationMode();
    const configured = repeatedEvaluation(config) ? `同模型多会话重复评审：${config.judges?.length || config.evaluation_repetitions || 5}个独立上下文会话。` : `${config.judges?.length || 0}个不同模型。`;
    $("models-state").textContent = `当前使用已保存的模型 API。${config.panel?.ready ? "评审配置完整：" + configured : "评审团配置未完整：" + (config.panel?.error || "请完成评审方式配置。")} 配置完整和连通成功分别记录。`;
  }
  function setModelsBusy(value) {
    modelsBusy = value;
    for (const id of ["save-models", "check-dialogue", "check-judges", "check-all-models", "evaluation-mode"]) $(id).disabled = value || batchBusy;
    $("add-judge-model").disabled = value || batchBusy || judgeEditors.length >= (modelConfiguration?.panel?.maximum || 9);
    renderEvaluationMode();
  }
  async function showModels() {
    $("models-panel").hidden = false; $("models-error").textContent = ""; $("models-state").textContent = "正在读取模型配置…"; $("models-panel").scrollIntoView({block: "start"}); setModelsBusy(true);
    try { renderModelConfiguration(await api("/api/admin/translation/models")); }
    catch (error) { $("models-error").textContent = errorText(error); }
    finally { setModelsBusy(false); }
  }
  async function saveModels() {
    if (modelsBusy) return; setModelsBusy(true); $("models-error").textContent = "";
    try {
      const mode = $("evaluation-mode").value, repetitions = Number($("evaluation-repetitions").value);
      if (!["distinct_models", "repeated_sessions"].includes(mode)) throw new Error("请选择有效评审方式。");
      if (mode === "repeated_sessions" && (!Number.isInteger(repetitions) || repetitions < 5 || repetitions > 9)) throw new Error("重复评审须使用5—9个独立上下文会话。");
      const judges = mode === "distinct_models" ? judgePayloads() : null;
      if (judges?.some(model => !model.id || !model.model || !model.base_url || !Number.isFinite(model.weight) || model.weight <= 0)) throw new Error("请完整填写每个模型的标识、名称、API地址和正数权重；完全空白的模型行不会保存。");
      const dialogue = {model: $("dialogue-model").value.trim(), base_url: $("dialogue-base-url").value.trim(), api_key: $("dialogue-api-key").value, temperature: Number($("dialogue-temperature").value), max_tokens: Number($("dialogue-max-tokens").value)};
      const payload = {evaluation_mode: mode};
      if (mode === "repeated_sessions") payload.evaluation_repetitions = repetitions;
      else payload.judges = judges;
      if (mode === "distinct_models" || dialogueDirty) payload.dialogue = dialogue;
      const saved = await api("/api/admin/translation/models", json("PUT", payload)); renderModelConfiguration(saved); $("model-check-results").replaceChildren(el("p", "配置已保存。请使用下方连通测试实际检查；评分按新批次独立记录。", "notice")); await refresh();
    } catch (error) { $("models-error").textContent = errorText(error); }
    finally { setModelsBusy(false); }
  }
  async function checkModels(target) {
    if (modelsBusy) return;
    if (modelsDirty) { $("models-error").textContent = "模型配置尚有未保存的修改，请先保存后再检查。"; return; }
    setModelsBusy(true); $("models-error").textContent = ""; $("model-check-results").replaceChildren(el("p", "正在向已保存的模型接口发送真实测试请求，请稍候…", "notice"));
    try {
      const checked = await api("/api/admin/translation/models/check", json("POST", {target})); renderModelConfiguration(checked);
      $("model-check-results").replaceChildren(table([repeatedEvaluation(checked) ? "会话 / 模型" : "模型", "检查结果", "说明"], (checked.checks || []).map(item => [repeatedEvaluation(checked) ? `${item.id || "会话"} · ${item.model || "—"}` : item.model || item.id, item.ok ? "调用成功" : "调用失败", item.ok ? time(item.verified_at) : item.error || "接口未返回有效结果"]))); await refresh();
    } catch (error) { $("models-error").textContent = errorText(error); $("model-check-results").replaceChildren(); }
    finally { setModelsBusy(false); }
  }
  async function evaluateBatch() {
    if (batchBusy || modelsBusy) return;
    $("batch-evaluation-error").textContent = "";
    if (modelsDirty) { $("batch-evaluation-error").textContent = "模型配置尚未保存，请先保存后再开始当前批次评审。"; return; }
    batchBusy = true; batchStopRequested = false; $("batch-evaluate-button").disabled = true;
    $("batch-stop-button").hidden = false; $("batch-stop-button").disabled = false;
    const allowReassessment = $("batch-allow-reassessment").checked;
    $("batch-allow-reassessment").disabled = true; setModelsBusy(modelsBusy);
    let completed = 0, preserved = 0;
    try {
      const fixed = await activeEvaluationBatch(), data = await api("/api/admin/translation/records");
      const targets = (Array.isArray(data) ? data : data.records || []).filter(record => record.record_kind === "participant" && record.ended_at && record.current_cohort !== false && record.cohort_id === fixed.cohort_id);
      for (const record of targets) {
        if (batchStopRequested) break;
        await activeEvaluationBatch(fixed);
        if (typeof record.submission !== "string") throw new Error("记录列表缺少已提交译文，已停止批量评审。");
        const hash = await submissionHash(record.submission), latest = await settledEvaluation(record.id, hash, fixed);
        if (latest.assessment && !assessmentForBatch(latest, fixed.evaluation_batch_id) && !allowReassessment) { preserved++; continue; }
        $("batch-evaluation-status").textContent = `正在核对并评审 ${completed + preserved + 1} / ${targets.length} 份译文；当前请求完成后才会处理下一份。`;
        await evaluateCurrentRecord(record.id, hash, fixed, allowReassessment);
        completed++;
        await activeEvaluationBatch(fixed);
      }
      $("batch-evaluation-status").textContent = `${batchStopRequested ? "已停止后续评审" : "批次处理完成"}：${completed} 份已确认完成当前批次评分，${preserved} 份原有其他批次评分保留。`;
    } catch (error) { $("batch-evaluation-error").textContent = errorText(error); $("batch-evaluation-status").textContent = `已停止：${completed} 份已确认完成，原始评分与服务端处理中请求均保留。`; }
    finally { batchBusy = false; $("batch-stop-button").hidden = true; $("batch-allow-reassessment").disabled = false; setModelsBusy(modelsBusy); await refresh(); }
  }
  async function refresh() {
    if (refreshing) return; refreshing = true; $("refresh-button").disabled = true; $("admin-status").textContent = "正在读取研究记录…"; $("admin-error").textContent = "";
    try { const [stats, data] = await Promise.all([api("/api/admin/translation/summary"), api("/api/admin/translation/records")]); summary = stats; records = Array.isArray(data) ? data : data.records || []; renderSummary(); renderRecords(); $("admin-status").textContent = `已更新 · ${new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date())} 北京时间`;
    } catch (error) { $("admin-error").textContent = errorText(error); $("admin-status").textContent = ""; }
    finally { refreshing = false; $("refresh-button").disabled = false; }
  }
  $("models-button").addEventListener("click", showModels); $("close-models").addEventListener("click", () => { $("models-panel").hidden = true; });
  $("save-models").addEventListener("click", saveModels); $("check-dialogue").addEventListener("click", () => checkModels("dialogue")); $("check-judges").addEventListener("click", () => checkModels("judges")); $("check-all-models").addEventListener("click", () => checkModels("all"));
  $("add-judge-model").addEventListener("click", () => { const draft = judgeEditors.map(editor => ({id: editor.id.value, label: editor.label.value, model: editor.model.value, base_url: editor.base_url.value, weight: Number(editor.weight.value), api_key: editor.api_key.value})); if (draft.length >= (modelConfiguration?.panel?.maximum || 9)) return; let id = 1; while (draft.some(model => model.id === `judge-${id}`)) id++; draft.push({id: `judge-${id}`, weight: 1}); modelsDirty = true; renderJudges(draft, true); });
  for (const id of ["dialogue-model", "dialogue-base-url", "dialogue-api-key", "dialogue-temperature", "dialogue-max-tokens"]) $(id).addEventListener("input", () => { modelsDirty = true; dialogueDirty = true; });
  $("evaluation-mode").addEventListener("change", () => { modelsDirty = true; renderEvaluationMode(); });
  $("evaluation-repetitions").addEventListener("input", () => { modelsDirty = true; });
  $("batch-evaluate-button").addEventListener("click", evaluateBatch);
  $("batch-stop-button").addEventListener("click", () => { batchStopRequested = true; $("batch-stop-button").disabled = true; $("batch-evaluation-status").textContent = "已请求停止；当前已接受的评分完成后将停止后续评审。"; });
  $("refresh-button").addEventListener("click", refresh); $("materials-button").addEventListener("click", showMaterials); $("close-materials").addEventListener("click", () => { $("materials-panel").hidden = true; });
  $("close-detail").addEventListener("click", () => { currentId = null; $("record-detail").hidden = true; });
  for (const id of ["cohort-filter", "kind-filter", "condition-filter", "assessment-filter"]) $(id).addEventListener("change", () => { page = 0; renderRecords(); });
  $("records-prev").addEventListener("click", () => { page--; renderRecords(); }); $("records-next").addEventListener("click", () => { page++; renderRecords(); });
  $("freeze-confirmed").addEventListener("change", () => { $("freeze-button").disabled = !$("freeze-confirmed").checked; });
  $("freeze-button").addEventListener("click", async () => {
    if (!$("freeze-confirmed").checked) return; $("freeze-button").disabled = true; $("freeze-error").textContent = "";
    try { await api("/api/admin/translation/materials/freeze", json("POST", {confirmed: true})); await refresh(); await showMaterials(); }
    catch (error) { $("freeze-error").textContent = errorText(error); $("freeze-button").disabled = false; }
  });
  $("logout-button").addEventListener("click", async () => { try { await api("/api/admin/logout", json("POST", {})); location.replace("/admin/login"); } catch (error) { $("admin-error").textContent = errorText(error); } });
  refresh().then(() => { if (location.pathname === "/admin/instruments") showMaterials(); });
})();
