// Run with: node tests/frontend_state.test.cjs
// Exercise the shipped event handlers and async state against a small DOM. This
// complements browser checks; layout and browser storage policies need a browser.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const staticDirectory = path.resolve(__dirname, "../static");
const html = fs.readFileSync(path.join(staticDirectory, "index.html"), "utf8");
const shippedScript = fs.readFileSync(path.join(staticDirectory, "app.js"), "utf8");
assert.match(shippedScript, /\ninit\(\);\s*$/);
// Suppress only automatic startup so each test can control network timing. The
// real init(), render functions, storage helpers and listeners run unchanged.
const controlledScript = shippedScript.replace(/\ninit\(\);\s*$/, "\n");

class Storage {
  constructor(values = {}) { this.values = new Map(Object.entries(values)); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toLowerCase();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this.value = "";
    this.type = "";
    this.name = "";
    this.id = "";
    this.textContent = "";
    this.hidden = false;
    this.checked = false;
    this.disabled = false;
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const enabled = force ?? !classes.has(name);
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
    };
  }
  append(...nodes) {
    for (const child of nodes) {
      if (child.parentElement) child.parentElement.children = child.parentElement.children.filter((node) => node !== child);
      child.parentElement = this;
      this.children.push(child);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this.append(...nodes);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }
  async emit(name, details = {}) {
    const event = { target: this, ...details, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; } };
    await Promise.all((this.listeners.get(name) || []).map((listener) => listener(event)));
    return event;
  }
  requestSubmit() {
    this.submitCount = (this.submitCount || 0) + 1;
    this.lastSubmitPromise = this.emit("submit");
    return this.lastSubmitPromise;
  }
  querySelectorAll(selector) {
    const matches = [];
    const selectors = selector.split(",").map((item) => item.trim());
    const visit = (node) => {
      for (const child of node.children) {
        if (selectors.some((item) => matchesSelector(child, item))) matches.push(child);
        visit(child);
      }
    };
    visit(this);
    return matches;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  scrollIntoView() {}
  focus() {}
}

function matchesSelector(element, selector) {
  const tag = selector.match(/^[\w-]+/)?.[0];
  if (tag && element.tagName !== tag) return false;
  if (selector.includes(":checked") && !element.checked) return false;
  for (const match of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
    const value = element[match[1]] || element.attributes.get(match[1]);
    if (match[2] == null ? value == null : value !== match[2]) return false;
  }
  return true;
}

function makeDocument() {
  const root = new Element("document");
  const stack = [root];
  const voidTags = new Set(["meta", "link", "input", "br", "hr", "img"]);
  for (const match of html.matchAll(/<(\/?)([\w-]+)([^>]*)>/g)) {
    const [, closing, rawTag, rawAttributes] = match;
    const tag = rawTag.toLowerCase();
    if (closing) {
      while (stack.length > 1 && stack.at(-1).tagName !== tag) stack.pop();
      if (stack.length > 1) stack.pop();
      continue;
    }
    const element = new Element(tag);
    for (const attribute of rawAttributes.matchAll(/([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
      const [, name, doubleQuoted, singleQuoted, unquoted] = attribute;
      const value = doubleQuoted ?? singleQuoted ?? unquoted ?? "";
      element.attributes.set(name, value);
      if (["hidden", "disabled", "checked"].includes(name)) element[name] = true;
      else if (["id", "type", "name", "value"].includes(name)) element[name] = value;
      else if (name.startsWith("data-")) element.dataset[name.slice(5)] = value;
    }
    stack.at(-1).append(element);
    if (!voidTags.has(tag)) stack.push(element);
  }
  root.createElement = (tag) => new Element(tag);
  root.getElementById = (id) => {
    let found = null;
    const visit = (node) => {
      if (node.id === id) found = node;
      if (!found) node.children.forEach(visit);
    };
    visit(root);
    return found;
  };
  root.documentElement = root.querySelector("html");
  return root;
}

const info = {
  age_bands: [{ id: "18-20", label: "18–20 岁", family: "adult" }, { id: "21-23", label: "21–23 岁", family: "adult" }, { id: "24-25", label: "24–25 岁", family: "adult" }],
  eligibility: {minimum_age: 18, maximum_age: 25, enrollment_required: true, education_stages: [{id: "college", label: "在读专科"}, {id: "bachelor", label: "在读本科"}]},
  study_mode: "pilot", design_version: "university-v1", clarification_strategy: "prompt",
  literacy_band_width: 10, model_config_id: "regression-model", answer_prompt_version: "regression-prompt",
  max_turns: 12, max_minutes: 30, has_active_task: true,
  access_code_required: false,
};
const form = {
  version: "student-literacy-v1-30",
  items: Array.from({ length: 30 }, (_, index) => ({
    id: `q${index + 1}`, domain: "信息核查", question: `判断题 ${index + 1}`,
    options: ["核对原文", "直接相信", "不确定"],
  })),
};
function session(id, overrides = {}) {
  return {
    id, design_version: info.design_version, run_mode: info.study_mode,
    clarification_strategy: info.clarification_strategy, literacy_band_width: info.literacy_band_width,
    model_config_id: info.model_config_id, prompt_version: info.answer_prompt_version,
    task: { title: `任务 ${id}`, instructions: "核查报名规则", shared_context: "虚构案例",
      participant_facts: "每周可参加两次", decision_options: [] },
    messages: [], completed_turns: 0, pending_reply: false, completed_request_ids: [],
    decision_stage: false, can_enter_decision: false, max_turns: 12, remaining_seconds: 600,
    created_at: new Date().toISOString(), ended_at: null, is_demo: true, ...overrides,
  };
}
function response(body) { return { ok: true, status: 200, json: async () => body }; }
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness({ localStorage = new Storage(), sessionStorage = new Storage(), fetch, bodyDataset = {} } = {}) {
  const document = makeDocument();
  document.body ||= document.querySelector("body");
  Object.assign(document.body.dataset, bodyDataset);
  const requests = [];
  const context = vm.createContext({
    document, localStorage, sessionStorage, crypto: webcrypto, TextEncoder, structuredClone,
    performance, Date, console, confirm: () => true, alert() {},
    window: { scrollTo() {} }, location: { reload() {} },
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1,
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (!fetch) throw new Error(`Unexpected fetch: ${url}`);
      return fetch(url, options);
    },
  });
  vm.runInContext(controlledScript, context, { filename: "static/app.js" });
  context.fixtureInfo = structuredClone(info);
  vm.runInContext("info = fixtureInfo", context);
  return {
    context, document, localStorage, sessionStorage, requests,
    el: (id) => { const element = document.getElementById(id); assert.ok(element, `Missing element #${id}`); return element; },
    run: (code) => vm.runInContext(code, context),
    value: (code) => JSON.parse(JSON.stringify(vm.runInContext(code, context))),
    put(name, value) { context[name] = structuredClone(value); },
  };
}

test("a new pilot clears questionnaire, consent and final-decision state while retaining the old record", async () => {
  const h = harness();
  h.put("oldSession", session("old"));
  h.run("sessionId = 'old'; currentSession = oldSession; currentAgeBand = '18-20'; pretestVersion = 'old-form'; pretestResponses = {q1: 1}; pretestItems = [{id: 'q1'}]; pretestPage = 4; startRequestId = 'old-start';");
  for (const id of ["age-band", "education-stage", "topic-familiarity", "access-code",
    "message-input", "submission-input", "burden-input", "helpfulness-input"]) h.el(id).value = "previous participant";
  for (const id of ["consent-check", "student-confirmed"]) h.el(id).checked = true;
  for (const key of ["lens_university_v1_pretest_draft_v1", "lens_university_v1_pending_message", "lens_university_v1_pending_start"]) h.sessionStorage.setItem(key, "{}");
  await h.run("beginNewSession()");
  assert.deepEqual(h.value("({ sessionId, currentAgeBand, pretestVersion, pretestResponses, pretestItems, pretestPage, startRequestId })"),
    { sessionId: null, currentAgeBand: "", pretestVersion: "", pretestResponses: {}, pretestItems: [], pretestPage: 0, startRequestId: null });
  for (const id of ["age-band", "education-stage", "topic-familiarity", "access-code",
    "message-input", "submission-input", "burden-input", "helpfulness-input"]) assert.equal(h.el(id).value, "", id);
  for (const id of ["consent-check", "student-confirmed"]) assert.equal(h.el(id).checked, false, id);
  for (const key of ["lens_university_v1_pretest_draft_v1", "lens_university_v1_pending_message", "lens_university_v1_pending_start"]) assert.equal(h.sessionStorage.getItem(key), null, key);
  assert.equal(JSON.parse(h.localStorage.getItem("lens_university_v1_session_history"))[0].id, "old");
  assert.equal(h.el("consent-panel").hidden, false);
  assert.equal(h.el("start-button").disabled, true);
});

test("pending question survives reload and is cleared only after that request completes", () => {
  const h = harness();
  const pending = { sessionId: "active", content: "报名截止时间？", requestId: "message-1" };
  h.sessionStorage.setItem("lens_university_v1_pending_message", JSON.stringify(pending));
  h.put("activeSession", session("active"));
  h.run("sessionId = 'active'; renderSession(activeSession)");
  assert.equal(h.el("message-input").value, pending.content);
  assert.notEqual(h.sessionStorage.getItem("lens_university_v1_pending_message"), null);
  h.put("finishedSession", session("active", { completed_turns: 1, completed_request_ids: ["message-1"] }));
  h.run("renderSession(finishedSession)");
  assert.equal(h.el("message-input").value, "");
  assert.equal(h.sessionStorage.getItem("lens_university_v1_pending_message"), null);
  h.sessionStorage.setItem("lens_university_v1_pending_message", JSON.stringify(pending));
  h.el("message-input").value = "我还要准备哪些材料？";
  h.run("renderSession(finishedSession)");
  assert.equal(h.el("message-input").value, "我还要准备哪些材料？", "sync must preserve a newly typed question");
});

test("out-of-order history responses cannot replace the latest selection or switch during a message", async () => {
  const waits = [];
  const h = harness({ fetch: () => { const wait = deferred(); waits.push(wait); return wait.promise; } });
  h.localStorage.setItem("lens_university_v1_session_history", JSON.stringify([{ id: "A", label: "A" }, { id: "B", label: "B" }]));
  h.put("activeSession", session("active"));
  h.run("sessionId = 'active'; renderSession(activeSession)");
  const buttons = h.el("session-history-list").querySelectorAll("button");
  const first = buttons[0].emit("click");
  const second = buttons[1].emit("click");
  waits[1].resolve(response(session("B"))); await second;
  waits[0].resolve(response(session("A"))); await first;
  assert.equal(h.run("sessionId"), "B");
  assert.equal(h.el("task-title").textContent, "任务 B");
  const delayed = h.el("session-history-list").querySelectorAll("button").at(-1).emit("click");
  h.run("messageBusy = true; updateControls()");
  waits[2].resolve(response(session("A"))); await delayed;
  assert.equal(h.run("sessionId"), "B");
  h.run("messageBusy = false");
  const beforeNewPilot = h.el("session-history-list").querySelectorAll("button").at(-1).emit("click");
  await h.run("beginNewSession()");
  waits[3].resolve(response(session("A"))); await beforeNewPilot;
  assert.equal(h.run("sessionId"), null);
  assert.equal(h.el("consent-panel").hidden, false);
});

test("editing intake while a saved questionnaire loads preserves its answers and restored page", async () => {
  const wait = deferred();
  const answers = Object.fromEntries(form.items.map((item) => [item.id, 0]));
  const draft = { ageBand: "18-20", version: form.version, answers, page: 3, education: "bachelor", familiarity: "3" };
  const storage = new Storage({ lens_university_v1_pretest_draft_v1: JSON.stringify(draft) });
  const h = harness({ sessionStorage: storage, fetch: () => wait.promise });
  h.el("age-band").value = "18-20";
  h.el("education-stage").value = "bachelor";
  h.el("topic-familiarity").value = "3";
  const loading = h.run("loadPretest({restore: true})");
  h.el("education-stage").value = "college";
  await h.el("education-stage").emit("change");
  assert.deepEqual(JSON.parse(storage.getItem("lens_university_v1_pretest_draft_v1")).answers, answers);
  wait.resolve(response(form)); await loading;
  assert.deepEqual(h.value("pretestResponses"), answers);
  assert.equal(h.run("pretestPage"), 3);
  assert.equal(JSON.parse(storage.getItem("lens_university_v1_pretest_draft_v1")).education, "college");
});

test("lost start response reuses its request ID after reload despite answer insertion order changing", async () => {
  const sharedSessionStorage = new Storage();
  const sharedLocalStorage = new Storage();
  const posted = [];
  const route = async (url, options) => {
    if (url === "/api/info") return response(info);
    if (url.startsWith("/api/pretest?")) return response(form);
    if (url === "/api/start") {
      posted.push(JSON.parse(options.body));
      throw new Error("Response lost after the server created the session");
    }
    throw new Error(`Unexpected fetch ${url}`);
  };
  const first = harness({ localStorage: sharedLocalStorage, sessionStorage: sharedSessionStorage, fetch: route });
  first.put("fixtureForm", form);
  first.put("reverseAnswers", Object.fromEntries([...form.items].reverse().map((item) => [item.id, 0])));
  first.run("pretestItems = fixtureForm.items; pretestVersion = fixtureForm.version; pretestResponses = reverseAnswers; pretestPage = 4; currentAgeBand = '18-20'");
  first.el("age-band").value = "18-20";
  first.el("education-stage").value = "bachelor";
  first.el("topic-familiarity").value = "3";
  first.el("consent-check").checked = true; first.el("student-confirmed").checked = true;
  first.run("saveDraft(); renderPretestPage()");
  await first.el("start-button").emit("click");
  const pendingBeforeReload = JSON.parse(sharedSessionStorage.getItem("lens_university_v1_pending_start"));
  assert.equal(posted.length, 1);
  assert.equal(posted[0].student_confirmed, true);
  assert.equal(posted[0].education_stage, "bachelor");
  assert.equal("guardian_consent" in posted[0], false);
  assert.ok(pendingBeforeReload.requestId);
  assert.equal(first.run("sessionId"), null);

  const reloaded = harness({ localStorage: sharedLocalStorage, sessionStorage: sharedSessionStorage, fetch: route });
  await reloaded.run("init()");
  assert.deepEqual(Object.keys(reloaded.value("pretestResponses")), form.items.map((item) => item.id));
  reloaded.el("consent-check").checked = true; reloaded.el("student-confirmed").checked = true;
  await reloaded.el("consent-check").emit("change");
  assert.equal(reloaded.el("start-button").disabled, false);
  await reloaded.el("start-button").emit("click");
  assert.equal(posted.length, 2);
  assert.equal(posted[1].request_id, posted[0].request_id, "retry must address the original server allocation");
  assert.equal(JSON.parse(sharedSessionStorage.getItem("lens_university_v1_pending_start")).payloadHash, pendingBeforeReload.payloadHash);
  assert.deepEqual(posted[1].pretest_answers, posted[0].pretest_answers);
});

test("model preview restores only its own service namespace and prefixes every API request", async () => {
  const suffix = ":%2Fpreview-api";
  const local = new Storage({
    lens_university_v1_session_id: "participant-kept", lens_university_v1_preview_session_id: "old-showcase-trial",
    [`lens_university_v1_preview_session_id${suffix}`]: "real-model-trial",
  });
  const drafts = new Storage({lens_university_v1_preview_pretest_draft_v1: '{"demo":"kept"}'});
  const h = harness({
    localStorage: local, sessionStorage: drafts,
    bodyDataset: {researcherPreview: "true", apiPrefix: "/preview-api/"},
    fetch: (url) => response(url.endsWith("/api/info?researcher_preview=true")
      ? info : url.endsWith("/api/session/real-model-trial") ? session("real-model-trial") : {}),
  });
  await h.run("init()");
  assert.deepEqual(h.requests.map((request) => request.url), [
    "/preview-api/api/info?researcher_preview=true", "/preview-api/api/session/real-model-trial",
  ]);
  assert.equal(h.run("sessionId"), "real-model-trial");
  assert.equal(h.el("mode-banner").textContent, "研究者试跑。");
  assert.deepEqual(h.value("[STORAGE_KEY, DRAFT_KEY, HISTORY_KEY, PENDING_MESSAGE_KEY, PENDING_START_KEY]"), [
    "lens_university_v1_preview_session_id", "lens_university_v1_preview_pretest_draft_v1", "lens_university_v1_preview_session_history",
    "lens_university_v1_preview_pending_message", "lens_university_v1_preview_pending_start",
  ].map((key) => key + suffix));
  for (const [url, method] of [
    ["/api/pretest?age_band=15-17", "GET"], ["/api/start", "POST"],
    ["/api/session/real-model-trial/message", "POST"], ["/api/session/real-model-trial/decision", "POST"],
    ["/api/session/real-model-trial/submit", "POST"], ["/api/session/real-model-trial", "DELETE"],
  ]) {
    h.put("apiPath", url); h.put("apiMethod", method);
    await h.run("api(apiPath, {method: apiMethod})");
    assert.equal(h.requests.at(-1).url, "/preview-api" + url);
    assert.equal(h.requests.at(-1).options.method, method);
  }
  await h.run("beginNewSession()");
  assert.equal(local.getItem("lens_university_v1_session_id"), "participant-kept");
  assert.equal(local.getItem("lens_university_v1_preview_session_id"), "old-showcase-trial");
  assert.equal(drafts.getItem("lens_university_v1_preview_pretest_draft_v1"), '{"demo":"kept"}');
});

test("ordinary participant ignores a preview API prefix and keeps its existing storage namespace", async () => {
  const h = harness({bodyDataset: {apiPrefix: "/preview-api"}, fetch: () => response(info)});
  await h.run("init()");
  assert.equal(h.requests[0].url, "/api/info");
  assert.equal(h.run("STORAGE_KEY"), "lens_university_v1_session_id");
  await h.run('api("/api/start", {method: "POST"})');
  assert.equal(h.requests.at(-1).url, "/api/start");
  assert.equal(h.el("mode-banner").textContent, "任务测试 · 先完成信息素养测试，再与 AI 查证资料。");
});

test("preview rejects unsafe prefixes instead of sending requests to an external or ambiguous URL", async () => {
  for (const apiPrefix of ["https://other.example", "//other.example", "/../other", "/preview?target=other",
    "/preview#other", "/preview\\other", "/preview%2Fother", "/preview//other", "/preview path"]) {
    const h = harness({bodyDataset: {researcherPreview: "true", apiPrefix}, fetch: () => response({})});
    await h.run('api("/api/start", {method: "POST"})');
    assert.equal(h.requests[0].url, "/api/start", apiPrefix);
    assert.equal(h.run("STORAGE_KEY"), "lens_university_v1_preview_session_id", apiPrefix);
  }
});

test("preview with no separate service keeps legacy keys and uses the same trial label", async () => {
  const h = harness({bodyDataset: {researcherPreview: "true"}, fetch: () => response({...info, study_mode: "demo"})});
  await h.run("init()");
  assert.equal(h.requests[0].url, "/api/info?researcher_preview=true");
  assert.equal(h.run("STORAGE_KEY"), "lens_university_v1_preview_session_id");
  assert.equal(h.el("mode-banner").textContent, "研究者试跑。");
});

function keyboardChat({ pendingMessage } = {}) {
  const posted = [];
  const h = harness({fetch: (url, options) => {
    if (url === "/api/session/keyboard/message") {
      posted.push(JSON.parse(options.body));
      return pendingMessage ? pendingMessage.promise : response({});
    }
    if (url === "/api/session/keyboard") return response(session("keyboard", {
      completed_turns: posted.length,
      completed_request_ids: posted.map((message) => message.request_id),
      messages: [{role: "user", content: posted.at(-1).content}, {role: "assistant", content: "需要报名表和参加计划。"}],
    }));
    throw new Error(`Unexpected keyboard-chat request: ${url}`);
  }});
  h.put("keyboardSession", session("keyboard"));
  h.run("sessionId = 'keyboard'; renderSession(keyboardSession)");
  h.el("message-input").value = "需要哪些材料？";
  return {h, posted, input: h.el("message-input"), form: h.el("chat-form")};
}

test("Enter sends one message through the existing form workflow and clears the accepted input", async () => {
  const {h, posted, input, form} = keyboardChat();
  const event = await input.emit("keydown", {key: "Enter"});
  await form.lastSubmitPromise;
  assert.equal(event.defaultPrevented, true, "sending must not also insert a newline");
  assert.equal(form.submitCount, 1);
  assert.equal(posted.length, 1);
  assert.equal(posted[0].content, "需要哪些材料？");
  assert.ok(posted[0].request_id, "keyboard sending must retain request deduplication");
  assert.equal(input.value, "");
  assert.equal(h.run("completedTurns"), 1);
});

test("Shift+Enter preserves the browser newline action and does not submit", async () => {
  const {posted, input, form} = keyboardChat();
  const event = await input.emit("keydown", {key: "Enter", shiftKey: true});
  assert.equal(event.defaultPrevented, false);
  assert.equal(form.submitCount || 0, 0);
  assert.equal(posted.length, 0);
  assert.equal(input.value, "需要哪些材料？");
});

test("Chinese IME confirmation is not sent, including composition tracking and keyCode 229", async () => {
  const {posted, input, form} = keyboardChat();
  await input.emit("compositionstart");
  const tracked = await input.emit("keydown", {key: "Enter", isComposing: false, keyCode: 13});
  await input.emit("compositionend");
  const composing = await input.emit("keydown", {key: "Enter", isComposing: true});
  const legacy = await input.emit("keydown", {key: "Enter", keyCode: 229});
  assert.equal(tracked.defaultPrevented, false);
  assert.equal(composing.defaultPrevented, false);
  assert.equal(legacy.defaultPrevented, false);
  assert.equal(posted.length, 0);
  assert.equal(form.submitCount || 0, 0);
  await input.emit("keydown", {key: "Enter", keyCode: 13});
  await form.lastSubmitPromise;
  assert.equal(posted.length, 1, "a deliberate Enter after composition still sends normally");
});

test("holding Enter does not send a repeated keydown or insert a newline", async () => {
  const {posted, input, form} = keyboardChat();
  const event = await input.emit("keydown", {key: "Enter", repeat: true});
  assert.equal(event.defaultPrevented, true);
  assert.equal(posted.length, 0);
  assert.equal(form.submitCount || 0, 0);
});

test("Enter does not submit whitespace or unavailable chat controls", async () => {
  for (const state of ["whitespace", "button-disabled", "input-disabled"]) {
    const {h, posted, input, form} = keyboardChat();
    if (state === "whitespace") input.value = " \n  ";
    if (state === "button-disabled") h.el("send-button").disabled = true;
    if (state === "input-disabled") input.disabled = true;
    await input.emit("keydown", {key: "Enter"});
    assert.equal(posted.length, 0, state);
    assert.equal(form.submitCount || 0, 0, state);
  }
});

test("a second Enter while the model is pending does not duplicate the request", async () => {
  const wait = deferred();
  const {h, posted, input, form} = keyboardChat({pendingMessage: wait});
  await input.emit("keydown", {key: "Enter"});
  assert.equal(posted.length, 1);
  assert.equal(h.el("send-button").disabled, true);
  await input.emit("keydown", {key: "Enter"});
  assert.equal(posted.length, 1);
  assert.equal(form.submitCount, 1);
  wait.resolve(response({}));
  await form.lastSubmitPromise;
  assert.equal(input.value, "");
  assert.equal(h.el("send-button").disabled, false);
});

test("university intake offers only the three adult age strata and two enrolled education stages", async () => {
  const h = harness({fetch: () => response({...structuredClone(info),
    age_bands: [...info.age_bands, {id: "12-14", label: "旧年龄组"}],
    eligibility: {...info.eligibility, education_stages: [...info.eligibility.education_stages, {id: "graduate", label: "旧学段"}]},
  })});
  await h.run("init()");
  assert.deepEqual(h.el("age-band").children.map(option => option.value), ["", "18-20", "21-23", "24-25"]);
  assert.deepEqual(h.el("education-stage").children.map(option => option.value), ["", "college", "bachelor"]);
  assert.ok(h.el("student-confirmed"));
  assert.doesNotMatch(html, /未成年人|监护人|家长|最高教育|小学|初中|高中|研究生及以上/);
});

test("a completed university form still requires enrollment confirmation and an eligible education stage", async () => {
  const h = harness();
  h.put("fixtureForm", form);
  h.put("allAnswers", Object.fromEntries(form.items.map(item => [item.id, 0])));
  h.run("pretestItems = fixtureForm.items; pretestVersion = fixtureForm.version; pretestResponses = allAnswers; currentAgeBand = '18-20'");
  h.el("education-stage").value = "bachelor";
  h.el("topic-familiarity").value = "3";
  h.el("consent-check").checked = true;
  h.run("updateStartButton()");
  assert.equal(h.el("start-button").disabled, true);
  assert.match(h.el("start-hint").textContent, /在读学生确认/);
  h.el("student-confirmed").checked = true;
  await h.el("student-confirmed").emit("change");
  assert.equal(h.el("start-button").disabled, false);
  h.el("education-stage").value = "graduate";
  await h.el("education-stage").emit("change");
  assert.equal(h.el("start-button").disabled, true);
  h.el("education-stage").value = "college";
  await h.el("education-stage").emit("change");
  assert.equal(h.el("start-button").disabled, false);
});

test("switching university age strata retains the common thirty answers and current page", async () => {
  const h = harness({fetch: () => response(form)});
  h.el("age-band").value = "18-20";
  await h.run("loadPretest()");
  h.put("savedAnswers", {q1: 0, q2: 2, q7: 1});
  h.run("pretestResponses = savedAnswers; pretestPage = 2; renderPretestPage(); saveDraft()");
  let confirmations = 0;
  h.context.confirm = () => { confirmations++; return false; };
  for (const age of ["21-23", "24-25"]) {
    h.el("age-band").value = age;
    await h.el("age-band").emit("change");
    assert.equal(h.run("currentAgeBand"), age);
    assert.equal(h.run("pretestVersion"), "student-literacy-v1-30");
    assert.deepEqual(h.value("pretestResponses"), {q1: 0, q2: 2, q7: 1});
    assert.equal(h.run("pretestPage"), 2);
    assert.equal(JSON.parse(h.sessionStorage.getItem("lens_university_v1_pretest_draft_v1")).ageBand, age);
  }
  assert.equal(h.requests.length, 1, "all ages reuse the already-loaded common instrument");
  assert.equal(confirmations, 0, "the shared form never asks to erase answers when changing age");
});

test("university-v1 starts fresh without reading or deleting historical cross-age browser records", async () => {
  const local = new Storage({lens_session_id: "historical-session", lens_session_history: '[{"id":"historical-session"}]'});
  const drafts = new Storage({lens_pretest_draft_v1: '{"version":"historical-form"}'});
  const h = harness({localStorage: local, sessionStorage: drafts});
  assert.equal(h.run("sessionId"), null);
  assert.equal(h.run("STORAGE_KEY"), "lens_university_v1_session_id");
  await h.run("beginNewSession()");
  assert.equal(local.getItem("lens_session_id"), "historical-session");
  assert.equal(local.getItem("lens_session_history"), '[{"id":"historical-session"}]');
  assert.equal(drafts.getItem("lens_pretest_draft_v1"), '{"version":"historical-form"}');
});


test("researcher trial banner remains neutral for every service mode", async () => {
  for (const study_mode of ["demo", "pilot", "live"]) {
    const h = harness({bodyDataset: {researcherPreview: "true"}, fetch: () => response({...info, study_mode})});
    await h.run("init()");
    assert.equal(h.el("mode-banner").textContent, "研究者试跑。");
    assert.doesNotMatch(h.el("mode-banner").textContent, /合成|模拟|演示|真实/);
  }
});
