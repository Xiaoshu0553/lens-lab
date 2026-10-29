const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/translation-admin.html'), 'utf8');
const classesSource = fs.readFileSync(path.join(__dirname, 'frontend_state.test.cjs'), 'utf8');
const classes = classesSource.slice(classesSource.indexOf('class Storage'), classesSource.indexOf('\nconst info ='));
const reply = body => ({ok: true, status: 200, json: async () => body});
const defaultSummary = {study_mode: 'pilot', panel_ready: false, counts: {}, cells: [], gaps: [], effects: []};
const config = () => ({study_mode: 'pilot', dialogue: {model: 'translation-ai', base_url: 'https://gateway.invalid/v1', api_key_configured: true, temperature: 0, max_tokens: 1600}, judges: [], panel: {ready: false, minimum: 5, maximum: 9, error: '须配置5个不同模型'}});
function harness(fetchImpl = () => reply(config())) {
  const {Storage, Element, makeDocument} = new Function('html', `${classes}\nreturn {Storage, Element, makeDocument};`)(html);
  const document = makeDocument(); document.body = document.querySelector('body');
  const requests = [], timers = new Map(); let timerId = 0;
  const context = vm.createContext({document, Node: Element, console, Date, Intl, AbortController,
    sessionStorage: new Storage(), localStorage: new Storage(), location: {pathname: '/admin', href: '', replace() {}},
    window: {confirm: () => true}, setTimeout: (fn, ms) => {const id = ++timerId; timers.set(id, {fn, ms}); return id;}, clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => {requests.push({url, options}); return fetchImpl(url, options);}});
  const source = fs.readFileSync(path.join(root, 'static/translation-admin.js'), 'utf8').replace('  refresh().then(() => { if (location.pathname === "/admin/instruments") showMaterials(); });',
    '  globalThis.hooks = {assessmentView, showModels, saveModels, checkModels, renderModelConfiguration, openRecord, editors: () => judgeEditors, prime: value => { summary = value; }};');
  vm.runInContext(source, context);
  context.hooks.prime(defaultSummary);
  return {hooks: context.hooks, requests, element: id => document.getElementById(id), storage: context.sessionStorage};
}
function text(node) { return [node.textContent, ...node.children.map(text)].join(' '); }
function fullJudges() { return Array.from({length: 5}, (_, i) => ({id: `j${i}`, label: `不同AI${i}`, model: `different-model-${i}`, base_url: 'https://gateway.invalid/v1', weight: 1, api_key_configured: true})); }

test('model configuration shows missing panel clearly and never fills an existing key', () => {
  const h = harness(); h.hooks.renderModelConfiguration({...config(), dialogue: {...config().dialogue, api_key: 'DUMMY-SERVER-SECRET'}, judges: [{...fullJudges()[0], api_key: 'DUMMY-JUDGE-SECRET'}]});
  assert.match(h.element('models-state').textContent, /配置未完整/);
  assert.equal(h.element('dialogue-api-key').value, '');
  assert.equal(h.hooks.editors().length, 5);
  assert.ok(h.hooks.editors().every(editor => editor.api_key.value === '' && editor.api_key.type === 'password'));
  assert.doesNotMatch(text(h.element('models-panel')), /DUMMY-SERVER-SECRET|DUMMY-JUDGE-SECRET/);
  assert.equal(h.storage.values.size, 0);
});

test('each AI assessment displays its complete dimensions, own total, weight and trimming', () => {
  const h = harness(); const shown = h.hooks.assessmentView({score: 84, sd: 7, range: 20, model_totals: {a: 79, b: 99}, trimmed_model_ids: ['b'], ratings: [
    {model_id: 'a', label: 'AI甲', model: 'model-a', weight: 2, total: 79, scores: {accuracy: 80, context: 75, fluency: 85, style: 80, completeness: 80}, evidence: ['保留指代'], critical_errors: []},
    {model_id: 'b', label: 'AI乙', model: 'model-b', weight: 1, total: 99, trimmed: true, scores: {accuracy: 99, context: 99, fluency: 99, style: 99, completeness: 99}, evidence: ['中文自然'], critical_errors: []}]});
  const content = text(shown); assert.match(content, /多模型共同量规/); assert.match(content, /AI甲 · model-a · 总分 79\.0/); assert.match(content, /AI乙 · model-b · 已去除极值/); assert.match(content, /预设模型权重/);
  for (const dim of ['语义准确', '语境与人物一致', '中文流畅', '文体与声音', '完整与克制']) assert.match(content, new RegExp(dim));
});

test('saving sends entered cloud credentials once and clears fields after masked response', async () => {
  let sent;
  const h = harness((url, options) => {
    if (options.method === 'PUT') { sent = JSON.parse(options.body); return reply({...config(), judges: fullJudges(), panel: {ready: true, minimum: 5, maximum: 9}}); }
    return reply(url.endsWith('/summary') ? defaultSummary : url.endsWith('/records') ? {records: []} : config());
  });
  h.hooks.renderModelConfiguration({...config(), judges: fullJudges()});
  h.element('dialogue-api-key').value = 'DUMMY-DIALOGUE-KEY'; h.hooks.editors()[0].api_key.value = 'DUMMY-JUDGE-KEY';
  await h.hooks.saveModels();
  assert.equal(sent.dialogue.api_key, 'DUMMY-DIALOGUE-KEY'); assert.equal(sent.judges[0].api_key, 'DUMMY-JUDGE-KEY');
  assert.equal(sent.judges.length, 5); assert.equal(h.element('dialogue-api-key').value, ''); assert.equal(h.hooks.editors()[0].api_key.value, ''); assert.equal(h.storage.values.size, 0);
});

test('connection check uses explicit saved target and reports individual failures', async () => {
  const h = harness((url, options) => url.endsWith('/models/check') ? reply({...config(), checks: [{id: 'j0', model: 'model-a', ok: true, verified_at: '2026-10-29T12:00:00Z'}, {id: 'j1', model: 'model-b', ok: false, error: '认证失败'}]}) : reply(url.endsWith('/summary') ? defaultSummary : {records: []}));
  h.hooks.renderModelConfiguration(config()); await h.hooks.checkModels('judges');
  const request = h.requests.find(item => item.url.endsWith('/models/check')); assert.deepEqual(JSON.parse(request.options.body), {target: 'judges'});
  const results = text(h.element('model-check-results')); assert.match(results, /model-a/); assert.match(results, /调用成功/); assert.match(results, /model-b/); assert.match(results, /认证失败/);
});

test('unsaved cloud configuration is not silently discarded by a connection check', async () => {
  const h = harness(); h.hooks.renderModelConfiguration(config()); h.element('dialogue-model').value = 'new-model'; await h.element('dialogue-model').emit('input'); await h.hooks.checkModels('all');
  assert.equal(h.requests.length, 0); assert.match(h.element('models-error').textContent, /请先保存/); assert.equal(h.element('dialogue-model').value, 'new-model');
});

test('incomplete panel cannot start multi-model evaluation from record detail', async () => {
  const h = harness(() => reply({id: 'r1', ended_at: '2026-10-29T12:00:00Z', study_mode: 'pilot', current_cohort: true, assessment_status: 'pending', messages: []}));
  await h.hooks.openRecord('r1', false);
  const buttons = h.element('detail-content').querySelectorAll('button'); const evaluate = buttons.find(button => button.textContent === '运行多模型统一盲评');
  assert.ok(evaluate); assert.equal(evaluate.disabled, true); assert.match(text(h.element('detail-content')), /至少5个不同模型/);
});
