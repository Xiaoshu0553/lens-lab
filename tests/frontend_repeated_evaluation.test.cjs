const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/translation-admin.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'static/translation-admin.js'), 'utf8');
const fixture = fs.readFileSync(path.join(__dirname, 'frontend_state.test.cjs'), 'utf8');
const classes = fixture.slice(fixture.indexOf('class Storage'), fixture.indexOf('\nconst info ='));
const response = (body, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => structuredClone(body)});
const summary = () => ({study_mode: 'pilot', cohort_id: 'cohort-a', evaluation_batch_id: 'batch-a', evaluation_mode: 'repeated_sessions', evaluation_session_count: 5, independent_model_count: 1, panel_ready: true, counts: {}, cells: [], gaps: [], effects: []});
const configured = () => Array.from({length: 5}, (_, index) => ({id: `old-${index}`, label: `已保存模型${index}`, model: `saved-model-${index}`, base_url: 'https://gateway.invalid/v1', weight: 1, api_key_configured: true}));
const config = () => ({study_mode: 'pilot', evaluation_mode: 'repeated_sessions', evaluation_repetitions: 5,
  dialogue: {model: 'same-ai', base_url: 'https://api.example.invalid/v1', temperature: 0, max_tokens: 2048, api_key_configured: true}, configured_judges: configured(),
  judges: Array.from({length: 5}, (_, index) => ({id: `session-${index}`, label: `评审会话${index+1}`, model: 'same-ai', weight: 1})), panel: {ready: true, minimum: 5, maximum: 9}});
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
function harness(handler = () => undefined) {
  const {Storage, Element, makeDocument} = new Function('html', `${classes}\nreturn {Storage, Element, makeDocument};`)(html);
  const document = makeDocument(); document.body = document.querySelector('body');
  const requests = [], timers = new Map(); let nextTimer = 0;
  const context = vm.createContext({document, Node: Element, crypto: webcrypto, TextEncoder, AbortController, Date, Intl,
    location: {pathname: '/admin', href: '', replace() {}}, window: {}, sessionStorage: new Storage(),
    setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, {fn, ms}); if (ms < 10000) queueMicrotask(() => { if (timers.has(id)) { timers.delete(id); fn(); } }); return id; }, clearTimeout(id) { timers.delete(id); },
    async fetch(url, options = {}) { requests.push({url, options}); const result = await handler(url, options); return result || response(url.endsWith('/summary') ? summary() : url.endsWith('/records') ? {records: []} : config()); }});
  vm.runInContext(source.replace('  refresh().then(() => { if (location.pathname === "/admin/instruments") showMaterials(); });',
    '  globalThis.hooks = {renderModelConfiguration, saveModels, assessmentView, renderSummary, evaluateBatch, openRecord, editors: () => judgeEditors, prime(value) { summary = value; }};'), context);
  context.hooks.prime(summary());
  return {hooks: context.hooks, requests, element: id => document.getElementById(id)};
}

test('repeated-session mode keeps saved different-model editors and never populates a key', () => {
  const h = harness(); h.hooks.renderModelConfiguration({...config(), dialogue: {...config().dialogue, api_key: 'PRIVATE-CANARY'}});
  assert.equal(h.element('evaluation-mode').value, 'repeated_sessions');
  assert.equal(h.element('distinct-judges-panel').hidden, true);
  assert.equal(h.element('evaluation-repetitions-label').hidden, false);
  assert.equal(h.hooks.editors()[0].model.value, 'saved-model-0');
  assert.equal(h.element('dialogue-api-key').value, '');
  assert.match(h.element('models-state').textContent, /同模型多会话重复评审/);
  assert.doesNotMatch(h.element('models-state').textContent, /5个不同模型|PRIVATE-CANARY/);
});

test('saving repeated mode reuses the existing API without copying keys or overwriting saved judges', async () => {
  let sent;
  const h = harness((url, options) => { if (options.method === 'PUT') { sent = JSON.parse(options.body); return response(config()); } });
  h.hooks.renderModelConfiguration(config()); await h.hooks.saveModels();
  assert.deepEqual(sent, {evaluation_mode: 'repeated_sessions', evaluation_repetitions: 5});
  assert.equal(h.hooks.editors()[0].model.value, 'saved-model-0');
});

for (const repetitions of [4, 10, 5.5]) test(`invalid repeated-session count ${repetitions} never reaches the server`, async () => {
  const h = harness(); h.hooks.renderModelConfiguration(config()); h.element('evaluation-repetitions').value = repetitions;
  await h.hooks.saveModels(); assert.equal(h.requests.length, 0); assert.match(h.element('models-error').textContent, /5—9/);
});

test('switching back saves the original different-model configurations, not effective repeated sessions', async () => {
  let sent;
  const h = harness((url, options) => { if (options.method === 'PUT') { sent = JSON.parse(options.body); return response({...config(), evaluation_mode: 'distinct_models'}); } });
  h.hooks.renderModelConfiguration(config()); h.element('evaluation-mode').value = 'distinct_models'; await h.element('evaluation-mode').emit('change');
  assert.equal(h.element('distinct-judges-panel').hidden, false); await h.hooks.saveModels();
  assert.equal(sent.evaluation_mode, 'distinct_models'); assert.equal(sent.judges[0].model, 'saved-model-0');
  assert.equal(sent.judges.length, 5); assert.ok(sent.judges.every(item => !item.id.startsWith('session-')));
});

test('repeated assessments show five dimensions per session and the shared-model limitation', () => {
  const h = harness(); const dimensions = {accuracy: 80, context: 81, fluency: 82, style: 83, completeness: 84};
  const shown = text(h.hooks.assessmentView({evaluation_mode: 'repeated_sessions', score: 82, model_totals: {s1: 82}, trimmed_model_ids: ['s2'], ratings: [{model_id: 's1', label: '会话1', model: 'same-ai', scores: dimensions, weight: 1, evidence: ['保留指代']}]}));
  assert.match(shown, /同模型多会话重复评审结果/); assert.match(shown, /去除最高 \/ 最低会话/);
  assert.match(shown, /共享模型偏差|仍共享模型偏差/);
  for (const dimension of ['语义准确', '语境与人物一致', '中文流畅', '文体与声音', '完整与克制']) assert.match(shown, new RegExp(dimension));
  assert.doesNotMatch(shown, /多模型共同量规盲评结果/);
});

test('statistics name sessions and translation-session duration accurately', () => {
  const h = harness(); h.hooks.prime({...summary(), counts: {total: 25}}); h.hooks.renderSummary();
  assert.match(text(h.element('counts')), /翻译会话数/); assert.match(text(h.element('cells')), /平均翻译会话时长/);
  assert.match(h.element('admin-mode').textContent, /同模型多会话重复评审/);
  assert.match(h.element('evaluation-limitation').textContent, /同一模型/);
  assert.doesNotMatch(text(h.element('counts')), /参与者人数/);
});

const record = (id, changes = {}) => ({id, cohort_id: 'cohort-a', current_cohort: true, record_kind: 'participant', ended_at: '2026-10-30T01:00:00Z', submission: '这是完整的最终译文。', assessment: null, evaluation_pending: null, ...changes});
test('batch evaluates only current submitted sessions, skips same-batch results and preserves other batches', async () => {
  const rows = {a: record('a', {evaluation_batch_id: 'batch-a', assessment: {evaluation_batch_id: 'batch-a', score: 80}}), b: record('b'), c: record('c', {evaluation_batch_id: 'old', assessment: {evaluation_batch_id: 'old', score: 70}}), p: record('p', {record_kind: 'researcher_preview'}), h: record('h', {current_cohort: false, cohort_id: 'historical'}), u: record('u', {ended_at: null})};
  const h = harness((url, options) => {
    if (url.endsWith('/summary')) return response(summary());
    if (url.endsWith('/records')) return response({records: Object.values(rows)});
    const id = url.split('/').at(-1) === 'evaluate' ? url.split('/').at(-2) : url.split('/').at(-1);
    if (options.method === 'POST') { const body = JSON.parse(options.body); assert.equal(body.evaluation_batch_id, 'batch-a'); assert.equal(body.submission_sha256, createHash('sha256').update(rows[id].submission).digest('hex')); assert.equal(body.allow_reassessment, false); rows[id].assessment = {evaluation_batch_id: 'batch-a', score: 80}; return response(rows[id].assessment); }
    return response(rows[id]);
  });
  await h.hooks.evaluateBatch();
  assert.deepEqual(h.requests.filter(item => item.options.method === 'POST').map(item => item.url), ['/api/admin/translation/records/b/evaluate']);
  assert.match(h.element('batch-evaluation-status').textContent, /2 份已确认完成.*1 份原有其他批次评分保留/);
  assert.equal(rows.c.assessment.score, 70);
});

test('batch stops on configuration changes and preserves the accepted first result', async () => {
  const active = summary(), rows = {a: record('a'), b: record('b')};
  const h = harness((url, options) => {
    if (url.endsWith('/summary')) return response(active);
    if (url.endsWith('/records')) return response({records: Object.values(rows)});
    const id = url.endsWith('/evaluate') ? url.split('/').at(-2) : url.split('/').at(-1);
    if (options.method === 'POST') { rows[id].assessment = {evaluation_batch_id: 'batch-a', score: 80}; active.evaluation_batch_id = 'batch-b'; return response(rows[id].assessment); }
    return response(rows[id]);
  });
  await h.hooks.evaluateBatch();
  assert.equal(h.requests.filter(item => item.options.method === 'POST').length, 1);
  assert.match(h.element('batch-evaluation-error').textContent, /评审批次或版本已变化/);
  assert.equal(rows.a.assessment.evaluation_batch_id, 'batch-a'); assert.equal(rows.b.assessment, null);
});

test('a submission changed after target selection is never sent for evaluation', async () => {
  const selected = record('a'), changed = {...selected, submission: selected.submission + '修改内容'};
  const h = harness((url) => url.endsWith('/summary') ? response(summary()) : url.endsWith('/records') ? response({records: [selected]}) : response(changed));
  await h.hooks.evaluateBatch();
  assert.equal(h.requests.filter(item => item.options.method === 'POST').length, 0);
  assert.match(h.element('batch-evaluation-error').textContent, /译文发生变化/);
});

test('accepted pending evaluation is synchronized to completion after a failed response without duplicate posts', async () => {
  const current = record('a'); let posted = false, recoveryGets = 0;
  const h = harness((url, options) => {
    if (url.endsWith('/summary')) return response(summary());
    if (url.endsWith('/records')) return response({records: [current]});
    if (options.method === 'POST') { posted = true; current.evaluation_pending = {evaluation_batch_id: 'batch-a'}; return response({detail: '请同步'}, 502); }
    if (posted && ++recoveryGets >= 2) { current.evaluation_pending = null; current.assessment = {evaluation_batch_id: 'batch-a', score: 80}; }
    return response(current);
  });
  await h.hooks.evaluateBatch();
  assert.equal(h.requests.filter(item => item.options.method === 'POST').length, 1);
  assert.equal(h.element('batch-evaluation-error').textContent, '');
  assert.match(h.element('batch-evaluation-status').textContent, /1 份已确认完成/);
});

test('server active assessment is reused after a configuration returns to its prior batch', async () => {
  const current = record('a', {evaluation_batch_id: 'other', assessment: {evaluation_batch_id: 'other', score: 70}, active_assessment: {evaluation_batch_id: 'batch-a', score: 80}});
  const h = harness((url) => url.endsWith('/summary') ? response(summary()) : url.endsWith('/records') ? response({records: [current]}) : response(current));
  await h.hooks.evaluateBatch();
  assert.equal(h.requests.filter(item => item.options.method === 'POST').length, 0);
  assert.match(h.element('batch-evaluation-status').textContent, /1 份已确认完成/);
  assert.equal(current.assessment.evaluation_batch_id, 'other');
});

test('single-record new-batch reassessment sends explicit permission and displays retained history', async () => {
  const old = {evaluation_batch_id: 'old', evaluation_mode: 'distinct_models', score: 70};
  const current = record('a', {assessment_status: 'completed', assessment: old});
  let sent;
  const h = harness((url, options) => {
    if (url.endsWith('/summary')) return response(summary());
    if (url.endsWith('/records')) return response({records: [current]});
    if (options.method === 'POST') { sent = JSON.parse(options.body); current.assessment_history = [old]; current.assessment = {evaluation_batch_id: 'batch-a', evaluation_mode: 'repeated_sessions', score: 80}; return response(current.assessment); }
    return response(current);
  });
  await h.hooks.openRecord('a', false);
  const button = h.element('detail-content').querySelectorAll('button').find(item => item.textContent === '用当前批次重新评审（保留原结果）');
  assert.ok(button); await button.emit('click');
  assert.equal(sent.allow_reassessment, true); assert.equal(sent.evaluation_batch_id, 'batch-a');
  assert.equal(sent.submission_sha256, createHash('sha256').update(current.submission).digest('hex'));
  assert.match(text(h.element('detail-content')), /历史评审批次 · 1/);
  assert.equal(current.assessment_history[0].score, 70);
});
