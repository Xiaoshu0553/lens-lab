// Run with: node --test tests/frontend_decisions.test.js
// Reuse the existing DOM fixture definitions without running or editing their
// tests. These tests execute the actual participant and administrator scripts.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const fixtureSource = fs.readFileSync(path.join(__dirname, 'frontend_state.test.cjs'), 'utf8');
const fixtureEnd = fixtureSource.indexOf('\ntest(');
assert.ok(fixtureEnd > 0, 'existing DOM fixture prelude is available');
function fixtures(preview = false) {
  let prelude = fixtureSource.slice(0, fixtureEnd);
  if (preview) prelude = prelude.replace('const document = makeDocument();',
    'const document = makeDocument(); document.body = document.querySelector("body"); document.body.dataset.researcherPreview = "true";');
  return new Function('require', '__dirname', `${prelude}\nreturn {harness, session, response, Storage};`)(require, __dirname);
}
const {harness, session, response, Storage} = fixtures();
const primary = Array.from({length: 5}, (_, index) => ({id: `opaque-plan-${index}`, text: `参加方案 ${index + 1}`}));
const execution = Array.from({length: 5}, (_, index) => ({id: `opaque-execution-${index}`, text: `办理方案 ${index + 1}`}));
function decisionSession(dual = true, ended = false) {
  return session('dual', {decision_stage: true, ended_at: ended ? new Date().toISOString() : null,
    task: {title: '候选任务', instructions: '分别作出两项决定', shared_context: '共同背景', participant_facts: '案例事实',
      decision_options: primary, ...(dual ? {execution_options: execution} : {})}});
}
function showSession(h, data) { h.put('testSession', data); h.run("sessionId = 'dual'; renderSession(testSession)"); }
async function choose(h, name, id) {
  const inputs = h.document.querySelectorAll(`input[name="${name}"]`);
  for (const input of inputs) input.checked = input.value === id;
  const selected = inputs.find((input) => input.value === id); assert.ok(selected); await selected.emit('change');
}
function content(element) { return [element.textContent, ...element.children.map(content)].filter(Boolean).join(' '); }

test('dual decisions require one independent choice from each group', async () => {
  const h = harness(); showSession(h, decisionSession());
  assert.equal(h.el('execution-plan-group').hidden, false);
  assert.equal(h.el('submit-button').disabled, true);
  assert.match(h.el('decision-choices-hint').textContent, /参加方案、办理方案/);
  await choose(h, 'decision-choice', primary[2].id);
  assert.equal(h.el('submit-button').disabled, true);
  assert.match(h.el('decision-choices-hint').textContent, /办理方案/);
  await choose(h, 'execution-choice', execution[4].id);
  assert.equal(h.el('submit-button').disabled, false);
  assert.equal(h.value('selectedDecision()'), primary[2].id);
  assert.equal(h.value('selectedExecution()'), execution[4].id);
  assert.match(content(h.el('execution-options')), /A.*B.*C.*D.*E/);
});

test('legacy single-choice tasks remain submittable without an execution choice', async () => {
  const h = harness(); showSession(h, decisionSession(false));
  assert.equal(h.el('execution-plan-group').hidden, true);
  await choose(h, 'decision-choice', primary[0].id);
  assert.equal(h.el('submit-button').disabled, false);
  assert.equal(h.value('selectedExecution()'), null);
});

test('session synchronization preserves both selected opaque tokens', async () => {
  const h = harness(); showSession(h, decisionSession());
  await choose(h, 'decision-choice', primary[1].id);
  await choose(h, 'execution-choice', execution[3].id);
  h.run('renderSession(testSession)');
  assert.equal(h.value('selectedDecision()'), primary[1].id);
  assert.equal(h.value('selectedExecution()'), execution[3].id);
  assert.equal(h.el('submit-button').disabled, false);
});

test('submission carries the separate execution token and waits for saved state', async () => {
  const ended = decisionSession(true, true);
  const h = harness({fetch: (url) => url.endsWith('/submit') ? response({}) : response(ended)});
  showSession(h, decisionSession());
  await choose(h, 'decision-choice', primary[0].id);
  await choose(h, 'execution-choice', execution[1].id);
  await h.el('submit-button').emit('click');
  const request = h.requests.find((entry) => entry.url.endsWith('/submit'));
  assert.ok(request);
  const body = JSON.parse(request.options.body);
  assert.equal(body.decision_choice_id, primary[0].id);
  assert.equal(body.execution_choice_id, execution[1].id);
  assert.equal(h.el('done-panel').hidden, false);
  assert.equal(h.el('done-copy').textContent, '任务已保存并自动评分。');
  assert.equal(h.el('new-pilot-button').textContent, '返回测试');
  assert.doesNotMatch(h.el('done-copy').textContent, /审核|预测试/);
});

test('researcher preview cannot resume or clear the participant storage namespace', async () => {
  const preview = fixtures(true);
  const local = new preview.Storage({lens_university_v1_session_id: 'participant', lens_university_v1_preview_session_id: 'trial'});
  const drafts = new preview.Storage({lens_university_v1_pretest_draft_v1: '{"participant":"kept"}', lens_university_v1_pending_message: '{"participant":"kept"}'});
  const h = preview.harness({localStorage: local, sessionStorage: drafts});
  assert.equal(h.value('sessionId'), 'trial');
  assert.equal(h.value('STORAGE_KEY'), 'lens_university_v1_preview_session_id');
  await h.run('beginNewSession()');
  assert.equal(local.getItem('lens_university_v1_session_id'), 'participant');
  assert.equal(drafts.getItem('lens_university_v1_pretest_draft_v1'), '{"participant":"kept"}');
  assert.equal(drafts.getItem('lens_university_v1_pending_message'), '{"participant":"kept"}');
});

function adminHarness(data = {}) {
  const participant = harness();
  const document = participant.document;
  for (const id of ['task-form', 'refresh-button', 'admin-age-band', 'pretest-analysis']) {
    const element = document.createElement(id === 'task-form' ? 'form' : 'div'); element.id = id; document.append(element);
  }
  document.getElementById('admin-age-band').value = '18-20';
  const context = vm.createContext({document, localStorage: new Storage(), console,
    fetch: async () => response(data)});
  const script = fs.readFileSync(path.join(__dirname, '../static/admin.js'), 'utf8')
    .replace(/\ninitAdmin\(\)\.catch\([^\n]+\);\s*$/, '\n');
  vm.runInContext(script, context, {filename: 'static/admin.js'});
  return {document, context, run: (code) => vm.runInContext(code, context)};
}

test('condition comparisons hide incomplete scenario aggregates and label between-person contrasts', () => {
  const h = adminHarness();
  const box = h.document.createElement('div'); h.context.box = box;
  h.context.data = {condition_comparison: {comparisons: [{literacy_band: '050-060', standard_n: 3, adaptive_n: 4,
    standard_mean: 71, adaptive_mean: 85, difference_points: 14, paired_scenarios: 2, required_scenarios: 4,
    complete: false, small_cell_warning: true}]}};
  h.run('renderConditionComparisons(box, data)');
  const body = box.querySelector('tbody');
  assert.deepEqual(body.children[0].children.map((item) => item.textContent).slice(0, 6), ['50–<60', '3', '—', '4', '—', '—']);
  assert.match(content(box), /2\/4个情境/);
  assert.match(content(box), /不同受试者的组间差/);
  assert.match(content(box), /等权/);
  assert.doesNotMatch(content(box), /NaN|undefined/);
});

test('new item diagnostics tolerate missing estimates and expose uncertainty limitations', async () => {
  const h = adminHarness({version: 'candidate', item_count: 30, n: 8, mean_percent: 45, kr20: null,
    sample_note: '样本不足，不能认定稳定区分度。', items: [{id: 'q1', domain: '信息核验', question: '题目',
      p_correct: null, p_correct_ci95: null, corrected_item_total_r: null, uncertain_count: 2, uncertain_rate: 0.25,
      high_low_discrimination: null, discrimination_unstable: true, options: [{index: 0, text: '候选项', count: 2, selection_rate: 0.25}]}]});
  await h.run('loadPretestAnalysis()');
  const text = content(h.document.getElementById('pretest-analysis'));
  assert.doesNotMatch(text, /样本不足/);
  assert.match(text, /25\.0%/);
  assert.match(text, /各选项选择情况/);
  assert.doesNotMatch(text, /NaN|undefined/);
});

test('older item diagnostics remain readable when new fields are absent', async () => {
  const h = adminHarness({version: 'legacy', item_count: 30, n: 2, mean_percent: 30, kr20: null,
    items: [{id: 'q1', domain: '信息核验', question: '题目', p_correct: 0.5, corrected_item_total_r: null, uncertain_count: 1}]});
  await h.run('loadPretestAnalysis()');
  const text = content(h.document.getElementById('pretest-analysis'));
  assert.match(text, /50\.0%/); assert.doesNotMatch(text, /样本不足/);
  assert.doesNotMatch(text, /NaN|undefined/);
});

test('structured questionnaire separates the question from material without omitting facts', () => {
  const h = harness();
  h.put('plainItem', {id:'u20',domain:'规则应用',question:'规则第一行。\n人物情况第二行。\n接下来应做什么？',options:['操作一','操作二','操作三','我暂时不会选']});
  h.run('pretestItems = [plainItem]; pretestPage = 0; renderPretestPage()');
  const field = h.el('pretest-items').querySelector('fieldset');
  assert.equal(field.querySelector('legend').textContent, '1. 接下来应做什么？');
  assert.equal(field.querySelector('p').textContent, '规则第一行。\n人物情况第二行。');
  assert.equal(field.attributes.get('aria-describedby'), 'material-u20');
  assert.equal(field.querySelectorAll('input').length, 4);
});

test('a one-line questionnaire item is displayed once', () => {
  const h = harness();
  h.put('plainItem', {id:'u19',domain:'规则应用',question:'按说明应何时归还？',options:['今天','明天','以后','我暂时不会选']});
  h.run('pretestItems = [plainItem]; pretestPage = 0; renderPretestPage()');
  const field = h.el('pretest-items').querySelector('fieldset');
  assert.equal(field.querySelector('legend').textContent, '1. 按说明应何时归还？');
  assert.equal(field.querySelector('p'), null);
});
