// Run with: node --test tests/frontend_translation.test.cjs
// Executes shipped handlers against the repository's lightweight DOM fixture.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const root = path.resolve(__dirname, '..');
const existingFixture = fs.readFileSync(path.join(__dirname, 'frontend_state.test.cjs'), 'utf8');
const fixtureClasses = existingFixture.slice(existingFixture.indexOf('class Storage'), existingFixture.indexOf('\nconst info ='));
const info = {study_mode: 'pilot', max_turns: 12, max_minutes: 30, ready: true, access_code_required: false};
const form = {version: 'translation-test', items: [{id: 'q1', stem: '译文判断', options: ['甲', '乙']}], self_items: []};
const response = body => ({ok: true, status: 200, json: async () => body});
function session(id = 'session-one', overrides = {}) {
  return {id, task: {title: '共同翻译任务', instructions: '英译中', source_text: 'This is the source.', brief: '小说', context_cards: []}, messages: [], draft: '', draft_saved_at: null, completed_turns: 0, completed_request_ids: [], remaining_seconds: 600, pending_reply: false, can_submit: false, ended_at: null, study_mode: 'pilot', ...overrides};
}
function harness({admin = false, preview = false, stored = {}, fetch = () => { throw new Error('Unexpected request'); }, pathname = '/admin'} = {}) {
  const basename = admin ? 'translation-admin' : 'translation';
  const html = fs.readFileSync(path.join(root, 'static', basename + '.html'), 'utf8');
  const {Storage, Element, makeDocument} = new Function('html', `${fixtureClasses}\nreturn {Storage, Element, makeDocument};`)(html);
  const document = makeDocument(); document.body = document.querySelector('body');
  if (preview) document.body.dataset.researcherPreview = 'true';
  const storage = new Storage(stored), timers = new Map(), requests = [];
  let nextTimer = 0;
  const context = vm.createContext({document, sessionStorage: storage, Node: Element, crypto: webcrypto, AbortController, console, Date, Intl,
    navigator: {clipboard: {writeText: async () => {}}},
    window: {confirm: () => true, addEventListener() {}}, location: {pathname, href: '', replace() {}},
    setInterval: () => 1,
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => { requests.push({url, options}); return fetch(url, options); }
  });
  let source = fs.readFileSync(path.join(root, 'static', basename + '.js'), 'utf8');
  if (admin) source = source.replace('  refresh().then(() => { if (location.pathname === "/admin/instruments") showMaterials(); });', `
    globalThis.hooks = {api, filteredRecords, assessmentView, showMaterials, renderRecords, renderSummary, openRecord,
      prime(data) { if (data.records) records = data.records; if (data.summary) summary = data.summary; }};`);
  else source = source.replace('  init();', `
    globalThis.hooks = {api, start, send, applyView, intakeReady, recover, schedulePoll,
      state: () => ({saved, messageRequest, busy, view, pollCount}),
      prime(data) { if (data.info) info = data.info; if (data.form) form = data.form; if (data.saved) saved = data.saved; if (data.answers) answers = data.answers; if (data.selfAnswers) selfAnswers = data.selfAnswers; }};`);
  vm.runInContext(source, context, {filename: basename + '.js'});
  if (!admin) context.hooks.prime({info: structuredClone(info), form: structuredClone(form)});
  const element = id => document.getElementById(id);
  return {hooks: context.hooks, element, storage, timers, requests};
}
function intake(h) {
  h.element('age-band').value = '19'; h.element('education-stage').value = 'year-2'; h.element('translation-experience').value = '2'; h.element('consent').checked = true; h.element('university-confirmed').checked = true;
}
async function fireTimer(h, delay) {
  const pair = [...h.timers.entries()].find(([, timer]) => timer.delay === delay); assert.ok(pair, `Expected ${delay}ms timer`); h.timers.delete(pair[0]); await pair[1].callback();
}
function allText(node) { return [node.textContent, ...node.children.map(allText)].join(' '); }

test('start persists a stable request ID without persisting the invitation code', async () => {
  const h = harness({fetch: () => response(session())}); intake(h); h.element('access-code').value = 'PRIVATE-INVITE-123';
  h.hooks.prime({answers: {q1: 0}, selfAnswers: {}}); await h.hooks.start();
  const submitted = JSON.parse(h.requests[0].options.body); assert.equal(submitted.access_code, 'PRIVATE-INVITE-123');
  assert.equal(submitted.age_band, '19'); assert.equal(submitted.education_stage, 'bachelor'); assert.equal(submitted.undergraduate_year, 'year-2');
  const stored = [...h.storage.values.values()].join(''); assert.ok(!stored.includes('PRIVATE-INVITE-123')); assert.ok(stored.includes(submitted.request_id));
});

test('participant intake rejects broad legacy age bands and missing undergraduate year', () => {
  const h = harness(); intake(h); assert.equal(h.hooks.intakeReady(), true);
  h.element('age-band').value = '18-20'; assert.equal(h.hooks.intakeReady(), false);
  h.element('age-band').value = '19'; h.element('education-stage').value = 'bachelor'; assert.equal(h.hooks.intakeReady(), false);
  h.element('education-stage').value = 'graduate'; assert.equal(h.hooks.intakeReady(), false);
});

test('native dropdown change activates intake and saves undergraduate background', async () => {
  const h = harness(); intake(h);
  await h.element('education-stage').emit('change');
  assert.equal(h.element('begin-form').disabled, false);
  const state = JSON.parse(h.storage.getItem('lens-translation-v1:participant:'));
  assert.equal(state.intake.undergraduate_year, 'year-2');
  assert.equal(state.intake.education_stage, 'bachelor');
  assert.equal(state.intake.eligibility_version, 'cumt-undergraduate-v1');
});

test('authorized preview can begin before live freezing and without an invitation', () => {
  const h = harness({preview: true}); intake(h); h.hooks.prime({info: {...info, study_mode: 'live', ready: false, access_code_required: true}});
  assert.equal(h.hooks.intakeReady(), true);
  const participant = harness(); intake(participant); participant.hooks.prime({info: {...info, study_mode: 'live', ready: false, access_code_required: true}});
  assert.equal(participant.hooks.intakeReady(), false);
});

test('draft restoration is scoped to its session and respects the newer server draft', () => {
  const h = harness(); h.hooks.prime({saved: {sessionId: 'old', draftSessionId: 'old', draft: 'other session draft', draftUpdatedAt: Date.now()}});
  h.hooks.applyView(session('new', {draft: 'server draft'}), true); assert.equal(h.element('draft-input').value, 'server draft');
  h.hooks.prime({saved: {sessionId: 'new', draftSessionId: 'new', draft: 'unsaved local draft', draftUpdatedAt: Date.parse('2026-10-29T10:02:00Z')}});
  h.hooks.applyView(session('new', {draft: 'older server draft', draft_saved_at: '2026-10-29T10:01:00Z'}), true); assert.equal(h.element('draft-input').value, 'unsaved local draft');
  h.hooks.applyView(session('new', {draft: 'newer server draft', draft_saved_at: '2026-10-29T10:03:00Z'}), true); assert.equal(h.element('draft-input').value, 'newer server draft');
  assert.equal(h.hooks.state().saved.draftConflict.text, 'unsaved local draft'); assert.equal(h.element('draft-conflict').hidden, false);
});

test('a hanging POST releases the form, retains its ID, and polls the accepted request', async () => {
  let requestId, signal;
  const h = harness({fetch: (url, options) => {
    if (url.endsWith('/message')) { requestId = JSON.parse(options.body).request_id; signal = options.signal; return new Promise(() => {}); }
    return response(session('session-one', {pending_reply: true, pending_request_id: requestId}));
  }});
  h.hooks.applyView(session(), true); h.element('message-input').value = '请将这段小说翻译成中文。';
  const sending = h.element('chat-form').emit('submit'); await fireTimer(h, 210000); await sending;
  assert.equal(signal.aborted, true); assert.equal(h.hooks.state().busy, false); assert.equal(h.hooks.state().messageRequest.id, requestId);
  assert.match(h.element('chat-error').textContent, /超时/); assert.equal(h.element('sync-button').hidden, false); assert.equal(h.element('send-button').disabled, true);
  assert.ok([...h.timers.values()].some(timer => timer.delay === 2500));
  h.hooks.applyView(session('session-one', {completed_turns: 1, completed_request_ids: [requestId], can_submit: true}));
  assert.equal(h.hooks.state().messageRequest, null); assert.equal(h.element('message-input').value, ''); assert.equal(h.element('chat-error').textContent, ''); assert.equal(h.element('send-button').disabled, false);
  assert.equal(h.timers.size, 0);
});

test('automatic pending polling stops once the saved reply is available', async () => {
  const h = harness({fetch: () => response(session('session-one', {completed_turns: 1, can_submit: true}))});
  h.hooks.applyView(session('session-one', {pending_reply: true}), true); await fireTimer(h, 2500);
  assert.equal(h.hooks.state().view.pending_reply, false); assert.equal(h.timers.size, 0); assert.equal(h.element('sync-button').hidden, true);
});

test('current-cohort record view excludes old model scores until explicitly selected', () => {
  const h = harness({admin: true}); h.element('cohort-filter').value = 'current'; h.element('kind-filter').value = 'all'; h.element('condition-filter').value = 'all'; h.element('assessment-filter').value = 'all';
  h.hooks.prime({records: [{id: 'current', current_cohort: true, score: 90}, {id: 'old-model', current_cohort: false, score: 99}]});
  assert.deepEqual(Array.from(h.hooks.filteredRecords(), record => record.id), ['current']);
  h.element('cohort-filter').value = 'historical'; assert.deepEqual(Array.from(h.hooks.filteredRecords(), record => record.id), ['old-model']);
});

test('admin records default to all versions while statistics remain scoped to the current version', () => {
  const html = fs.readFileSync(path.join(root, 'static', 'translation-admin.html'), 'utf8');
  const choices = html.match(/<select id="cohort-filter">([\s\S]*?)<\/select>/)[1];
  assert.deepEqual(Array.from(choices.matchAll(/<option value="([^"]*)"/g), match => match[1]), ['all', 'current', 'historical']);
  assert.match(html, /以下按当前版本的翻译会话计数/);
  assert.match(html, /研究者试跑和历史版本分开保存/);
  const h = harness({admin: true});
  for (const id of ['cohort-filter', 'kind-filter', 'condition-filter', 'assessment-filter']) h.element(id).value = 'all';
  h.hooks.prime({records: [{id: 'current', current_cohort: true}, {id: 'previous', current_cohort: false}]});
  assert.deepEqual(Array.from(h.hooks.filteredRecords(), record => record.id), ['current', 'previous']);
});

test('admin record list shows configuration scope without reading record runtime mode', () => {
  const h = harness({admin: true});
  for (const id of ['cohort-filter', 'kind-filter', 'condition-filter', 'assessment-filter']) h.element(id).value = 'all';
  const current = {id: 'current-record', current_cohort: true, record_kind: 'participant', condition: 'A'};
  Object.defineProperty(current, 'study_mode', {get() { throw new Error('Record runtime mode must not be read'); }});
  h.hooks.prime({records: [current, {id: 'historical-record', current_cohort: false, record_kind: 'researcher_preview', condition: 'S'}]});
  h.hooks.renderRecords();
  const rendered = allText(h.element('record-list'));
  assert.match(rendered, /配置版本/); assert.match(rendered, /当前配置/); assert.match(rendered, /历史配置/);
  assert.doesNotMatch(rendered, /运行模式|study_mode|pilot|live|demo/);
});

test('admin summary keeps API and evaluation details without runtime mode labels', () => {
  const h = harness({admin: true});
  for (const study_mode of ['demo', 'pilot', 'live']) {
    h.hooks.prime({summary: {study_mode, model_api_verified: true, panel_ready: true, evaluation_mode: 'repeated_sessions', evaluation_session_count: 5, counts: {}, cells: [], gaps: [], effects: []}});
    h.hooks.renderSummary();
    const rendered = h.element('admin-mode').textContent;
    assert.doesNotMatch(rendered, /运行模式|预测试模式|正式实验模式|演示模式|study_mode|pilot|live|demo/);
    assert.match(rendered, /已记录成功 API 调用/); assert.match(rendered, /同模型多会话重复评审/);
  }
});

test('admin detail omits nested runtime fields while preserving evaluation mode and current evaluation controls', async () => {
  const record = {id: 'current-record', current_cohort: true, record_kind: 'participant', ended_at: '2026-10-30T01:00:00Z', study_mode: 'demo', assessment_status: 'pending', messages: [],
    model_config: {study_mode: 'private-runtime-marker', evaluation_mode: 'repeated_sessions'}, events: [{type: 'start', runtime_mode: 'private-runtime-marker', run_mode: 'private-runtime-marker'}]};
  const h = harness({admin: true, fetch: () => response({record})});
  h.hooks.prime({summary: {study_mode: 'pilot', panel_ready: true, evaluation_mode: 'repeated_sessions'}});
  await h.hooks.openRecord(record.id, false);
  const rendered = allText(h.element('detail-content'));
  assert.equal(h.element('detail-error').textContent, '');
  assert.doesNotMatch(rendered, /运行模式|study_mode|run_mode|runtime_mode|private-runtime-marker|"demo"/);
  assert.match(rendered, /evaluation_mode/); assert.match(rendered, /repeated_sessions/);
  const evaluate = h.element('detail-content').querySelectorAll('button').find(button => button.textContent === '运行同模型多会话重复评审');
  assert.ok(evaluate); assert.equal(evaluate.disabled, false);
  assert.equal(record.study_mode, 'demo'); assert.equal(record.events[0].runtime_mode, 'private-runtime-marker');
});

test('expert detail reads weighted totals from the aggregate result', () => {
  const h = harness({admin: true}); const rendered = h.hooks.assessmentView({score: 82, sd: 3, range: 10, trimmed_expert_ids: [], dimensions: {}, ratings: [{expert_id: 'faithfulness', scores: {}, evidence: ['语义准确'], critical_errors: []}], expert_totals: {faithfulness: 83.5}});
  assert.match(allText(rendered), /83\.5/);
});

test('pilot readiness is not displayed as frozen material approval', async () => {
  const h = harness({admin: true, fetch: () => response({ready: true, frozen: false, material_status: '候选材料', form: {items: [], self_items: []}, task: {}, rubric: [], experts: []})}); h.hooks.prime({summary: {study_mode: 'pilot'}}); await h.hooks.showMaterials();
  assert.match(h.element('materials-status').textContent, /未冻结/); assert.equal(h.element('materials-freeze').hidden, true);
});

test('materials panel displays all configured models from the materials API response', async () => {
  const models = Array.from({length: 5}, (_, index) => ({id: `judge-${index + 1}`, label: `独立评审 ${index + 1}`, model: `configured-model-${index + 1}`, weight: index + 1}));
  const h = harness({admin: true, fetch: url => {
    assert.equal(url, '/api/admin/translation/materials');
    return response({ready: true, frozen: false, material_status: '候选材料', form: {items: [], self_items: []}, task: {}, rubric: [], models});
  }});
  h.hooks.prime({summary: {study_mode: 'pilot'}}); await h.hooks.showMaterials();
  const text = allText(h.element('materials-content'));
  for (const model of models) { assert.ok(text.includes(model.label)); assert.ok(text.includes(model.model)); }
  assert.equal(h.element('materials-status').textContent.includes('失败'), false);
});


test('baseline researcher preview offers only direct and active clarification conditions', () => {
  const html = fs.readFileSync(path.join(root, 'static', 'translation.html'), 'utf8');
  const choices = html.match(/<select id="preview-condition">([\s\S]*?)<\/select>/)[1];
  assert.deepEqual(Array.from(choices.matchAll(/<option value="([^"]*)"/g), match => match[1]), ['', 'S', 'A']);
  const adminHtml = fs.readFileSync(path.join(root, 'static', 'translation-admin.html'), 'utf8');
  assert.doesNotMatch(adminHtml, /<option value="O">/);
  assert.match(adminHtml, /尚未开展优化模块/);
  assert.match(adminHtml, /每个不同的评审模型/);
});

test('baseline summary renders two conditions and reports API verification truthfully', () => {
  const h = harness({admin: true});
  h.hooks.prime({summary: {study_mode: 'pilot', model_api_verified: false, counts: {}, cells: [], gaps: [], effects: []}});
  h.hooks.renderSummary();
  const cellsText = allText(h.element('cells')), effectsText = allText(h.element('effects'));
  assert.match(cellsText, /S · 直接翻译/); assert.match(cellsText, /A · 主动澄清/);
  assert.doesNotMatch(cellsText, /O ·|优化/); assert.doesNotMatch(effectsText, /优化收益/);
  assert.match(h.element('admin-mode').textContent, /尚未记录成功 API 调用/);
  h.hooks.prime({summary: {study_mode: 'pilot', model_api_verified: true, counts: {}, cells: [], gaps: [], effects: []}});
  h.hooks.renderSummary(); assert.match(h.element('admin-mode').textContent, /已记录成功 API 调用/);
});
