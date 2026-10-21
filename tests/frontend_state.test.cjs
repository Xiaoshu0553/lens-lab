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
  async emit(name) {
    const event = { target: this, preventDefault() {} };
    await Promise.all((this.listeners.get(name) || []).map((listener) => listener(event)));
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
  age_bands: [{ id: "18-24", label: "18–24 岁", family: "adult" }],
  study_mode: "pilot", design_version: "regression-design", clarification_strategy: "prompt",
  literacy_band_width: 10, model_config_id: "regression-model", answer_prompt_version: "regression-prompt",
  max_turns: 12, max_minutes: 30, has_active_task: true,
  access_code_required: false, minor_access_code_required: false,
};
const form = {
  version: "regression-form",
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
function harness({ localStorage = new Storage(), sessionStorage = new Storage(), fetch } = {}) {
  const document = makeDocument();
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
  h.run("sessionId = 'old'; currentSession = oldSession; currentAgeBand = '18-24'; pretestVersion = 'old-form'; pretestResponses = {q1: 1}; pretestItems = [{id: 'q1'}]; pretestPage = 4; startRequestId = 'old-start';");
  for (const id of ["age-band", "education-stage", "topic-familiarity", "access-code", "minor-access-code",
    "message-input", "submission-input", "burden-input", "helpfulness-input"]) h.el(id).value = "previous participant";
  for (const id of ["consent-check", "guardian-consent", "participant-assent"]) h.el(id).checked = true;
  for (const key of ["lens_pretest_draft_v1", "lens_pending_message", "lens_pending_start"]) h.sessionStorage.setItem(key, "{}");
  await h.run("beginNewSession()");
  assert.deepEqual(h.value("({ sessionId, currentAgeBand, pretestVersion, pretestResponses, pretestItems, pretestPage, startRequestId })"),
    { sessionId: null, currentAgeBand: "", pretestVersion: "", pretestResponses: {}, pretestItems: [], pretestPage: 0, startRequestId: null });
  for (const id of ["age-band", "education-stage", "topic-familiarity", "access-code", "minor-access-code",
    "message-input", "submission-input", "burden-input", "helpfulness-input"]) assert.equal(h.el(id).value, "", id);
  for (const id of ["consent-check", "guardian-consent", "participant-assent"]) assert.equal(h.el(id).checked, false, id);
  for (const key of ["lens_pretest_draft_v1", "lens_pending_message", "lens_pending_start"]) assert.equal(h.sessionStorage.getItem(key), null, key);
  assert.equal(JSON.parse(h.localStorage.getItem("lens_session_history"))[0].id, "old");
  assert.equal(h.el("consent-panel").hidden, false);
  assert.equal(h.el("start-button").disabled, true);
});

test("pending question survives reload and is cleared only after that request completes", () => {
  const h = harness();
  const pending = { sessionId: "active", content: "报名截止时间？", requestId: "message-1" };
  h.sessionStorage.setItem("lens_pending_message", JSON.stringify(pending));
  h.put("activeSession", session("active"));
  h.run("sessionId = 'active'; renderSession(activeSession)");
  assert.equal(h.el("message-input").value, pending.content);
  assert.notEqual(h.sessionStorage.getItem("lens_pending_message"), null);
  h.put("finishedSession", session("active", { completed_turns: 1, completed_request_ids: ["message-1"] }));
  h.run("renderSession(finishedSession)");
  assert.equal(h.el("message-input").value, "");
  assert.equal(h.sessionStorage.getItem("lens_pending_message"), null);
  h.sessionStorage.setItem("lens_pending_message", JSON.stringify(pending));
  h.el("message-input").value = "我还要准备哪些材料？";
  h.run("renderSession(finishedSession)");
  assert.equal(h.el("message-input").value, "我还要准备哪些材料？", "sync must preserve a newly typed question");
});

test("out-of-order history responses cannot replace the latest selection or switch during a message", async () => {
  const waits = [];
  const h = harness({ fetch: () => { const wait = deferred(); waits.push(wait); return wait.promise; } });
  h.localStorage.setItem("lens_session_history", JSON.stringify([{ id: "A", label: "A" }, { id: "B", label: "B" }]));
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
  const draft = { ageBand: "18-24", version: form.version, answers, page: 3, education: "bachelor", familiarity: "3" };
  const storage = new Storage({ lens_pretest_draft_v1: JSON.stringify(draft) });
  const h = harness({ sessionStorage: storage, fetch: () => wait.promise });
  h.el("age-band").value = "18-24";
  h.el("education-stage").value = "bachelor";
  h.el("topic-familiarity").value = "3";
  const loading = h.run("loadPretest({restore: true})");
  h.el("education-stage").value = "graduate";
  await h.el("education-stage").emit("change");
  assert.deepEqual(JSON.parse(storage.getItem("lens_pretest_draft_v1")).answers, answers);
  wait.resolve(response(form)); await loading;
  assert.deepEqual(h.value("pretestResponses"), answers);
  assert.equal(h.run("pretestPage"), 3);
  assert.equal(JSON.parse(storage.getItem("lens_pretest_draft_v1")).education, "graduate");
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
  first.run("pretestItems = fixtureForm.items; pretestVersion = fixtureForm.version; pretestResponses = reverseAnswers; pretestPage = 4; currentAgeBand = '18-24'");
  first.el("age-band").value = "18-24";
  first.el("education-stage").value = "bachelor";
  first.el("topic-familiarity").value = "3";
  first.el("consent-check").checked = true;
  first.run("saveDraft(); renderPretestPage()");
  await first.el("start-button").emit("click");
  const pendingBeforeReload = JSON.parse(sharedSessionStorage.getItem("lens_pending_start"));
  assert.equal(posted.length, 1);
  assert.ok(pendingBeforeReload.requestId);
  assert.equal(first.run("sessionId"), null);

  const reloaded = harness({ localStorage: sharedLocalStorage, sessionStorage: sharedSessionStorage, fetch: route });
  await reloaded.run("init()");
  assert.deepEqual(Object.keys(reloaded.value("pretestResponses")), form.items.map((item) => item.id));
  reloaded.el("consent-check").checked = true;
  await reloaded.el("consent-check").emit("change");
  assert.equal(reloaded.el("start-button").disabled, false);
  await reloaded.el("start-button").emit("click");
  assert.equal(posted.length, 2);
  assert.equal(posted[1].request_id, posted[0].request_id, "retry must address the original server allocation");
  assert.equal(JSON.parse(sharedSessionStorage.getItem("lens_pending_start")).payloadHash, pendingBeforeReload.payloadHash);
  assert.deepEqual(posted[1].pretest_answers, posted[0].pretest_answers);
});
