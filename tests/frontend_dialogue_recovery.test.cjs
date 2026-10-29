// Run with: node --test tests/frontend_dialogue_recovery.test.cjs
// Execute the participant script with the shared DOM fixture and controlled I/O.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const fixtureSource = fs.readFileSync(path.join(__dirname, "frontend_state.test.cjs"), "utf8");
const fixtureEnd = fixtureSource.indexOf("\ntest(");
const {harness, session, response, deferred} = new Function("require", "__dirname",
  `${fixtureSource.slice(0, fixtureEnd)}\nreturn {harness, session, response, deferred};`)(require, __dirname);
const pendingKey = "lens_university_v1_pending_message";
const question = "报名需要哪些材料？";
function active(h, overrides = {}) {
  h.put("activeRecord", session("active", overrides));
  h.run("sessionId = 'active'; renderSession(activeRecord)");
}
function completed(requestId) {
  return session("active", {completed_turns: 1, completed_request_ids: [requestId],
    messages: [{role: "user", content: question}, {role: "assistant", content: "请准备报名表和在读证明。"}]});
}
async function fireTimer(h, delay) {
  const entry = [...h.timers.entries()].find(([, timer]) => timer.delay === delay);
  assert.ok(entry, `Expected a ${delay} ms timer`);
  h.timers.delete(entry[0]);
  await entry[1].callback();
}

test("a lost POST response is shown as success once the saved reply is confirmed", async () => {
  let requestId;
  const h = harness({fetch: (url, options) => {
    if (url.endsWith("/message")) {
      requestId = JSON.parse(options.body).request_id;
      throw new Error("response lost");
    }
    return response(completed(requestId));
  }});
  active(h); h.el("message-input").value = question;
  await h.el("chat-form").emit("submit");
  assert.equal(h.run("completedTurns"), 1);
  assert.equal(h.el("chat-error").textContent, "");
  assert.match(h.el("chat-status").textContent, /已回复/);
  assert.equal(h.el("message-input").value, "");
  assert.equal(h.sessionStorage.getItem(pendingKey), null);
  assert.equal(h.el("sync-button").hidden, true);
  assert.equal(h.el("send-button").disabled, false);
});

test("accepted POST plus a failed refresh retains the draft and request ID for safe retry", async () => {
  const posted = [];
  let online = false;
  const h = harness({fetch: (url, options) => {
    if (url.endsWith("/message")) { posted.push(JSON.parse(options.body)); return response({}); }
    if (!online) throw new Error("connection lost during refresh");
    return response(completed(posted[0].request_id));
  }});
  active(h); h.el("message-input").value = question;
  await h.el("chat-form").emit("submit");
  const saved = JSON.parse(h.sessionStorage.getItem(pendingKey));
  assert.equal(saved.requestId, posted[0].request_id);
  assert.equal(h.el("message-input").value, question);
  assert.equal(h.run("messageBusy"), false);
  assert.doesNotMatch(h.el("chat-status").textContent, /正在回复/);
  online = true;
  await h.el("chat-form").emit("submit");
  assert.equal(posted.length, 2);
  assert.equal(posted[1].request_id, posted[0].request_id);
  assert.equal(h.sessionStorage.getItem(pendingKey), null);
  assert.equal(h.run("completedTurns"), 1);
});

test("a hanging model request times out, releases the form, and retains the question", async () => {
  let signal;
  const h = harness({fetch: (url, options) => {
    if (url.endsWith("/message")) { signal = options.signal; return new Promise(() => {}); }
    return response(session("active"));
  }});
  active(h); h.el("message-input").value = question;
  const sending = h.el("chat-form").emit("submit");
  assert.equal(h.el("send-button").disabled, true);
  await fireTimer(h, 210000);
  await sending;
  assert.equal(signal.aborted, true);
  assert.match(h.el("chat-error").textContent, /等待超时/);
  assert.equal(h.run("messageBusy"), false);
  assert.equal(h.el("send-button").disabled, false);
  assert.equal(h.el("message-input").value, question);
  assert.ok(h.sessionStorage.getItem(pendingKey));
  assert.equal(h.timers.size, 0);
});

test("a timed-out POST still pending on the server is polled instead of submitted again", async () => {
  let requestId, syncs = 0;
  const h = harness({fetch: (url, options) => {
    if (url.endsWith("/message")) {
      requestId = JSON.parse(options.body).request_id;
      return new Promise(() => {});
    }
    return response(syncs++ === 0 ? session("active", {pending_reply: true, pending_request_id: requestId}) : completed(requestId));
  }});
  active(h); h.el("message-input").value = question;
  const sending = h.el("chat-form").emit("submit");
  await fireTimer(h, 210000); await sending;
  assert.equal(h.run("messageBusy"), false);
  assert.equal(h.el("send-button").disabled, true);
  assert.equal(h.el("chat-error").textContent, "");
  assert.match(h.el("chat-status").textContent, /自动同步/);
  await h.el("chat-form").emit("submit");
  assert.equal(h.requests.filter(({url}) => url.endsWith("/message")).length, 1);
  await fireTimer(h, 1800);
  assert.equal(h.run("completedTurns"), 1);
  assert.equal(h.el("send-button").disabled, false);
  assert.equal(h.sessionStorage.getItem(pendingKey), null);
});

test("API timeout also covers an unresponsive response body", async () => {
  const h = harness({fetch: () => ({ok: true, json: () => new Promise(() => {})})});
  const waiting = assert.rejects(h.run('api("/api/info")'), /等待超时/);
  await fireTimer(h, 30000);
  await waiting;
  assert.equal(h.timers.size, 0);
});

test("pending-reply polling reconnects after a transient failure and clears the error", async () => {
  let online = false;
  const h = harness({fetch: () => {
    if (!online) throw new Error("temporary network failure");
    return response(completed("pending-1"));
  }});
  h.sessionStorage.setItem(pendingKey, JSON.stringify({sessionId: "active", content: question, requestId: "pending-1"}));
  active(h, {pending_reply: true, pending_request_id: "pending-1"});
  await fireTimer(h, 1800);
  assert.match(h.el("chat-error").textContent, /自动重连/);
  assert.equal(h.el("message-input").disabled, true);
  online = true;
  await fireTimer(h, 5000);
  assert.equal(h.run("completedTurns"), 1);
  assert.equal(h.el("message-input").disabled, false);
  assert.equal(h.el("chat-error").textContent, "");
  assert.equal(h.sessionStorage.getItem(pendingKey), null);
  assert.equal(h.timers.size, 0);
});

test("polling a failed turn tells the participant to retry without losing the question", async () => {
  const h = harness({fetch: () => response(session("active"))});
  h.sessionStorage.setItem(pendingKey, JSON.stringify({sessionId: "active", content: question, requestId: "pending-1"}));
  active(h, {pending_reply: true});
  await fireTimer(h, 1800);
  assert.match(h.el("chat-error").textContent, /未收到回复.*请重试/);
  assert.equal(h.el("message-input").value, question);
  assert.equal(h.el("send-button").disabled, false);
  assert.equal(h.run("completedTurns"), 0);
});

test("a slow older sync cannot remove a reply returned by a newer sync", async () => {
  const waits = [deferred(), deferred()];
  let calls = 0;
  const h = harness({fetch: () => waits[calls++].promise});
  active(h);
  const older = h.run("syncSession()");
  const newer = h.run("syncSession()");
  waits[1].resolve(response(completed("new"))); await newer;
  waits[0].resolve(response(session("active"))); await older;
  assert.equal(h.run("completedTurns"), 1);
  assert.equal(h.run("currentSession.messages.at(-1).content"), "请准备报名表和在读证明。");
});

test("a newer sync failure does not suppress a successful older sync", async () => {
  const waits = [deferred(), deferred()];
  let calls = 0;
  const h = harness({fetch: () => waits[calls++].promise});
  active(h);
  const older = h.run("syncSession()");
  const newer = assert.rejects(h.run("syncSession()"), /网络连接失败/);
  waits[1].reject(new Error("newer connection failed")); await newer;
  waits[0].resolve(response(completed("new"))); await older;
  assert.equal(h.run("completedTurns"), 1);
});

test("overlapping polling and a failed manual sync keep reconnecting the pending turn", async () => {
  const waits = [deferred(), deferred()];
  let calls = 0;
  const h = harness({fetch: () => calls < 2 ? waits[calls++].promise : response(completed("pending-1"))});
  h.sessionStorage.setItem(pendingKey, JSON.stringify({sessionId: "active", content: question, requestId: "pending-1"}));
  active(h, {pending_reply: true});
  const polling = fireTimer(h, 1800);
  const manual = h.el("sync-button").emit("click");
  waits[0].resolve(response(session("active", {pending_reply: true}))); await polling;
  waits[1].reject(new Error("manual connection failed")); await manual;
  assert.equal(h.el("send-button").disabled, true);
  await fireTimer(h, 5000);
  assert.equal(h.run("completedTurns"), 1);
  assert.equal(h.el("send-button").disabled, false);
  assert.equal(h.sessionStorage.getItem(pendingKey), null);
});

test("leaving and returning to a session invalidates a sync started before navigation", async () => {
  const wait = deferred();
  const h = harness({fetch: () => wait.promise});
  active(h);
  const oldSync = h.run("syncSession()");
  h.run("navigationSequence += 2");
  h.put("latest", completed("latest"));
  h.run("renderSession(latest)");
  wait.resolve(response(session("active"))); await oldSync;
  assert.equal(h.run("completedTurns"), 1);
});

test("successful HTTP responses containing invalid JSON cannot silently discard a question", async () => {
  const h = harness({fetch: (url) => url.endsWith("/message")
    ? {ok: true, json: async () => { throw new Error("HTML proxy response"); }}
    : response(session("active"))});
  active(h); h.el("message-input").value = question;
  await h.el("chat-form").emit("submit");
  assert.match(h.el("chat-error").textContent, /格式异常/);
  assert.equal(h.el("message-input").value, question);
  assert.ok(h.sessionStorage.getItem(pendingKey));
  assert.equal(h.el("send-button").disabled, false);
});

test("incomplete or mismatched session snapshots leave the last valid conversation intact", async () => {
  for (const body of [{id: "active"}, session("someone-else"), session("active", {
    messages: [{role: "assistant", content: " "}], completed_turns: 1,
  })]) {
    const h = harness({fetch: () => response(body)});
    active(h);
    await assert.rejects(h.run("syncSession()"), /对话记录不完整|会话编号不一致/);
    assert.equal(h.run("currentSession.id"), "active");
    assert.equal(h.run("completedTurns"), 0);
    assert.equal(h.el("send-button").disabled, false);
  }
});

test("a changed clarification prompt disables an old session even with the same base prompt", () => {
  const h = harness();
  h.run('info.prompt_versions = {standard: info.answer_prompt_version, adaptive: info.answer_prompt_version + "+clarify-v5"}');
  active(h, {interaction_condition: 'adaptive', prompt_version: 'regression-prompt+clarify-v4'});
  assert.equal(h.run('compatibleSession'), false);
  assert.equal(h.el('send-button').disabled, true);
  assert.equal(h.el('resume-notice').hidden, false);
  assert.equal(h.el('restart-button').hidden, false);
});
