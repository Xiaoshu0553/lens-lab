// Run with: node --test tests/frontend_admin_records.test.js
// Reuse the small DOM fixture and execute the shipped administrator script.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const fixtureSource = fs.readFileSync(path.join(__dirname, 'frontend_state.test.cjs'), 'utf8');
const fixtureEnd = fixtureSource.indexOf('\ntest(');
assert.ok(fixtureEnd > 0, 'existing DOM fixture prelude is available');
const fixturePrelude = fixtureSource.slice(0, fixtureEnd).replace('"index.html"', '"admin.html"');
const {makeDocument, Storage, response} = new Function('require', '__dirname',
  `${fixturePrelude}\nreturn {makeDocument, Storage, response};`)(require, __dirname);
const shippedScript = fs.readFileSync(path.join(__dirname, '../static/admin.js'), 'utf8')
  .replace(/\ninitAdmin\(\)\.catch\([^\n]+\);\s*$/, '\n');

function adminHarness(rows, fetchImpl = null) {
  const document = makeDocument();
  const storage = new Storage();
  const requests = [];
  const context = vm.createContext({document, localStorage: storage, console,
    fetch: async (url, options = {}) => {
      requests.push({url, options});
      if (fetchImpl) return fetchImpl(url, options);
      assert.equal(url, '/api/admin/review-queue');
      return response(rows);
    }});
  vm.runInContext(shippedScript, context, {filename: 'static/admin.js'});
  return {document, requests, run: (code) => vm.runInContext(code, context),
    list: () => document.getElementById('review-list')};
}
function content(element) {
  return [element.textContent, ...element.children.map(content)].filter(Boolean).join(' ');
}
function record(overrides = {}) {
  return {id: 'record-123456789', task_id: 7, age_band: '18-20', is_demo: true, run_mode: 'pilot',
    created_at: '2026-10-19T15:59:59+00:00', decision_started_at: '2026-10-19T16:00:59+00:00',
    ended_at: '2026-10-19T16:01:29+00:00', reviewed_at: '2026-10-19T16:02:29+00:00', task_completion_seconds: 90,
    dialogue_elapsed_seconds: 60, decision_elapsed_seconds: 30,
    in_current_cohort: true, cohort_exclusion_reasons: [],
    literacy_instrument_version: 'adult-form-v3', model_config_id: 'model-config-42',
    prompt_version: 'prompt-v8', review_status: 'pending', reviewer: '', review_code: '', review_reason: '',
    flags: [], messages: [], machine_scored: true, score: null, submission: '',
    reference_text: '统一参考资料', rubric: '预设评分规则', ...overrides};
}
function metadata(card) {
  return card.querySelectorAll('details').find((details) =>
    /时间.*版本/.test(details.querySelector('summary').textContent));
}

test('record timestamps show Beijing time across midnight independently of the browser timezone', async () => {
  const originalTimezone = process.env.TZ;
  try {
    for (const timezone of ['UTC', 'America/Los_Angeles', 'Asia/Tokyo']) {
      process.env.TZ = timezone;
      const h = adminHarness([record()]);
      await h.run('loadReviews()');
      const text = content(h.list());
      assert.match(text, /提交 2026-10-20 00:01:29（北京时间）/);
      assert.match(text, /任务开始：2026-10-19 23:59:59/);
      assert.match(text, /进入决策：2026-10-20 00:00:59/);
      assert.match(text, /最终提交：2026-10-20 00:01:29/);
      assert.doesNotMatch(text, /最近审核|待审核/);
      assert.doesNotMatch(text, /2026-10-19T|\+00:00/);
    }
  } finally {
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
  }
});

test('an explicit non-UTC timestamp is rendered as the same instant in Beijing', async () => {
  const h = adminHarness([record({ended_at: '2026-10-20T00:01:29+08:00'})]);
  await h.run('loadReviews()');
  assert.match(content(h.list()), /提交 2026-10-20 00:01:29（北京时间）/);
});

test('missing, invalid and ambiguous timestamps remain unavailable instead of inventing a date', async () => {
  for (const timestamp of [null, undefined, '', 'not-a-time', '2026-02-30T18:12:34+00:00', '2026-10-19T18:12:34']) {
    const h = adminHarness([record({created_at: timestamp, decision_started_at: timestamp,
      ended_at: timestamp, reviewed_at: timestamp})]);
    await h.run('loadReviews()');
    const text = content(h.list());
    assert.match(text, /提交 —/);
    assert.match(text, /任务开始：—/);
    assert.match(text, /进入决策：—/);
    assert.match(text, /最终提交：—/);
    assert.doesNotMatch(text, /最近审核|待审核/);
    assert.doesNotMatch(text, /Invalid Date|NaN|undefined|null|not-a-time|1970/);
  }
});

test('completed records are read-only and show automatic scores regardless of legacy review status', async () => {
  for (const review_status of ['pending', 'valid', 'invalid']) {
    const h = adminHarness([record({review_status, score: 80, decision_choice_text: '参加方案乙',
      execution_choice_text: '办理方案甲', reviewer: 'demo-fixture',
      messages: [{role: 'user', content: '核对规则'}, {role: 'assistant', content: '资料内容'}]})]);
    await h.run('loadReviews()');
    const text = content(h.list());
    assert.match(text, /已完成/);
    assert.match(text, /自动评分：80\.0 分/);
    assert.match(text, /参加方案：参加方案乙/);
    assert.match(text, /办理方案：办理方案甲/);
    assert.match(text, /参与者：核对规则.*AI：资料内容/);
    assert.equal(h.list().querySelectorAll('input,select,button').length, 0);
    assert.doesNotMatch(text, /待审核|保存审核|保存评分|demo-fixture/);
    assert.deepEqual(h.requests.map(request => request.url), ['/api/admin/review-queue']);
    assert.ok(h.requests.every(request => !request.options.method || request.options.method === 'GET'));
  }
});

test('record analysis membership uses the automatic selector rather than the old cohort flag', async () => {
  const h = adminHarness([record({analysis_eligible: false, in_current_cohort: true,
    analysis_exclusion_reasons: ['历史任务版本'], cohort_exclusion_reasons: []})]);
  await h.run('loadReviews()');
  assert.match(content(h.list()), /不计入当前统计：历史任务版本/);
});

test('synthetic showcase eligibility remains separate from real-study analysis eligibility', async () => {
  const rows = [record({analysis_eligible:false,showcase_analysis_eligible:true,
    analysis_exclusion_reasons:['合成记录不纳入真实研究分析']}),
    record({id:'historical-synthetic',analysis_eligible:false,showcase_analysis_eligible:false,
      showcase_analysis_exclusion_reasons:['历史材料版本']})];
  const h = adminHarness(rows);
  h.run('configureAdminEnvironment({showcase_mode:true})');
  await h.run('loadReviews()');
  const cards = h.list().querySelectorAll('article');
  const current = cards.find(card => content(card).includes('展示统计可用'));
  assert.ok(current);
  assert.match(content(current), /展示统计可用 · 合成记录不纳入真实研究分析/);
  assert.doesNotMatch(content(current), /不计入当前统计|无效/);
  assert.ok(cards.some(card => /不计入当前展示统计：历史材料版本/.test(content(card))));

  const realStudy = adminHarness([rows[0]]);
  await realStudy.run('loadReviews()');
  assert.match(content(realStudy.list()), /不计入当前统计：合成记录不纳入真实研究分析/);
  assert.doesNotMatch(content(realStudy.list()), /展示统计可用/);
});

test('pretest QC flags have readable Chinese labels without changing record eligibility', async () => {
  const h = adminHarness([record({analysis_eligible:true,
    flags:['pretest_fast_completion','pretest_long_identical_run']})]);
  await h.run('loadReviews()');
  const text = content(h.list());
  assert.match(text, /信息素养测试用时偏短/);
  assert.match(text, /连续选择同一位置较多/);
  assert.match(text, /过程提醒.*信息素养测试用时偏短.*连续选择同一位置较多/);
  assert.match(text, /AI主动提示：0次（澄清0次、引导0次）/);
  assert.match(text, /当前统计范围/);
  assert.doesNotMatch(text, /pretest_fast_completion|pretest_long_identical_run|不计入当前统计/);
});

test('process alerts show the stored answer-vector evidence separately from AI prompt counts', async () => {
  const h = adminHarness([record({analysis_eligible:true,ai_prompt_count:3,ai_clarification_count:2,ai_guidance_count:1,
    process_alert_details:[{code:'pretest_long_identical_run',message:'原30题答案中最长连续同位置30题，达到12题',
      observed_value:30,threshold:12,source_fields:['literacy_answers_json'],quality_version:'observed-v1'}]})]);
  await h.run('loadReviews()');
  const text = content(h.list());
  assert.match(text, /连续同一位置30题（阈值12题）/);
  assert.match(text, /AI主动提示：3次（澄清2次、引导1次）/);
  assert.match(text, /当前统计范围/);
  assert.equal(h.list().querySelectorAll('div').filter(element => element.className === 'flag-line').length,1);
  assert.doesNotMatch(text, /literacy_answers_json|quality_version|observed-v1|不计入当前统计|无效/);
});

test('process-alert JSON is readable and preserves unsupported timing provenance in its message', async () => {
  const message = '模拟前测用时83秒；此项来自合成时长，非平台实测';
  const h = adminHarness([record({process_alert_details_json:JSON.stringify([
    {code:'pretest_fast_completion',message,observed_value:83,threshold:120},
    {code:'no_evidence_retrieved',message:'已提交任务，但资料获取记录为0条'}])})]);
  await h.run('loadReviews()');
  const text = content(h.list());
  assert.match(text, /模拟前测用时83秒；此项来自合成时长，非平台实测/);
  assert.match(text, /已提交任务，但资料获取记录为0条/);
  assert.doesNotMatch(text, /30题完成用时83秒（规则<120秒）/);
});

test('no triggered process alerts stay muted and do not reuse obsolete stored flags', async () => {
  const h = adminHarness([record({flags:['very_fast_completion'],process_alert_details:[]})]);
  await h.run('loadReviews()');
  const text = content(h.list());
  assert.match(text, /未触发过程提醒/);
  assert.doesNotMatch(text, /完成过快|过程提醒：无/);
  assert.equal(h.list().querySelectorAll('div').filter(element => element.className === 'flag-line').length,0);
  const line = h.list().querySelectorAll('p').find(element => element.textContent === '未触发过程提醒');
  assert.equal(line.className,'muted');
});

test('AI prompt fallback counts only assistant clarification and guidance actions', async () => {
  const h = adminHarness([record({flags:['very_fast_completion'],messages:[
    {role:'user',action:'task_guidance',content:'请帮我看看'},
    {role:'assistant',action:'module_clarification',content:'每周能来几次？'},
    {role:'assistant',action:'prompt_clarification',content:'以前参加过吗？'},
    {role:'assistant',action:'task_guidance',content:'请分别选择参加与办理方案。'},
    {role:'assistant',action:'evidence',content:'这里是规则原文。'},
    {role:'assistant',action:'answer',content:'普通答复。'}]})]);
  await h.run('loadReviews()');
  const text = content(h.list());
  assert.match(text, /AI主动提示：3次（澄清2次、引导1次）/);
  assert.match(text, /过程提醒.*完成过快/);
  assert.match(text, /参与者：请帮我看看/);
  assert.match(text, /AI（澄清）：每周能来几次？/);
  assert.match(text, /AI（澄清）：以前参加过吗？/);
  assert.match(text, /AI（引导）：请分别选择参加与办理方案。/);
  assert.match(text, /AI：这里是规则原文。/);
  assert.doesNotMatch(text, /自动提示：|AI（引导）：请帮我看看/);
});

test('backend AI prompt counts take precedence including explicit zero values', async () => {
  const h = adminHarness([record({ai_prompt_count:0,ai_clarification_count:0,ai_guidance_count:0,
    messages:[{role:'assistant',action:'module_clarification',content:'旧接口缓存消息'}]})]);
  await h.run('loadReviews()');
  assert.match(content(h.list()), /AI主动提示：0次（澄清0次、引导0次）/);
  assert.match(content(h.list()), /未触发过程提醒/);
  const declared = adminHarness([record({ai_prompt_count:5,ai_clarification_count:2,ai_guidance_count:3})]);
  await declared.run('loadReviews()');
  assert.match(content(declared.list()), /AI主动提示：5次（澄清2次、引导3次）/);
});

test('AI prompt summary uses all records in showcase and current age in ordinary mode', () => {
  const h = adminHarness([]);
  const data = `{ai_prompt_totals:{ai_prompt_count:3,ai_clarification_count:2,ai_guidance_count:1,records_with_ai_prompt:2,record_count:14},
    ai_prompt_totals_all_ages:{ai_prompt_count:88,ai_clarification_count:49,ai_guidance_count:39,records_with_ai_prompt:66,record_count:126}}`;
  h.run(`renderAiPromptSummary(${data})`);
  assert.match(content(h.document.getElementById('ai-prompt-summary')), /当前年龄：2\/14条对话包含AI主动提示，共3次：澄清2次、引导1次/);
  h.run(`configureAdminEnvironment({showcase_mode:true});renderAiPromptSummary(${data})`);
  assert.match(content(h.document.getElementById('ai-prompt-summary')), /全部记录：66\/126条对话包含AI主动提示，共88次：澄清49次、引导39次/);
});

test('AI prompt summary distinguishes missing data, no records and measured zero prompts', () => {
  const h = adminHarness([]);
  h.run('renderAiPromptSummary({})');
  assert.match(content(h.document.getElementById('ai-prompt-summary')), /当前年龄暂无AI主动提示统计/);
  h.run('renderAiPromptSummary({ai_prompt_totals:{ai_prompt_count:0,ai_clarification_count:0,ai_guidance_count:0,records_with_ai_prompt:0,record_count:0}})');
  assert.match(content(h.document.getElementById('ai-prompt-summary')), /暂无可统计的已完成对话，AI主动提示共0次/);
  h.run('renderAiPromptSummary({ai_prompt_totals:{ai_prompt_count:0,ai_clarification_count:0,ai_guidance_count:0,records_with_ai_prompt:0,record_count:126}})');
  const text = content(h.document.getElementById('ai-prompt-summary'));
  assert.match(text, /0\/126条对话包含AI主动提示，共0次：澄清0次、引导0次/);
  assert.doesNotMatch(text, /NaN|undefined|null|88/);
});

test('historical records remain in the queue with their current-statistics exclusion reasons', async () => {
  const h = adminHarness([record(), record({id: 'historical-123456', run_mode: 'demo',
    in_current_cohort: false, cohort_exclusion_reasons: ['运行模式不同', '问卷版本不同']})]);
  await h.run('loadReviews()');
  assert.equal(h.list().children.length, 2);
  const current = h.list().children.find(card => content(card).includes('当前统计范围'));
  assert.match(content(current), /当前统计范围/);
  assert.match(content(current), /PILOT/);
  assert.doesNotMatch(content(current), /DEMO/);
  const historical = content(h.list().children.find(card => content(card).includes('运行模式不同')));
  assert.match(historical, /不计入当前统计/);
  assert.match(historical, /运行模式不同.*问卷版本不同/);
});

test('record timing and version details stay collapsed and never expose credentials', async () => {
  const h = adminHarness([record({score: 99, decision_choice_text: '方案甲',
    api_key: 'secret-api-key', model_endpoint: 'secret-endpoint'})]);
  await h.run('loadReviews()');
  const details = metadata(h.list().children[0]);
  assert.ok(details, 'record metadata can be expanded');
  assert.ok(!details.open, 'record metadata starts collapsed');
  const text = content(details);
  assert.match(text, /任务总耗时：90\.0 秒/);
  assert.match(text, /对话阶段：60\.0 秒/);
  assert.match(text, /决策阶段：30\.0 秒/);
  assert.match(text, /adult-form-v3/);
  assert.match(text, /model-config-42/);
  assert.match(text, /提示词版本：prompt-v8/);
  assert.doesNotMatch(content(h.list()), /secret-api-key|secret-endpoint/);
  assert.match(content(h.list()), /自动评分：99\.0 分/);
});

test('prompt version availability no longer depends on a manual review state', async () => {
  for (const review_status of ['pending', 'valid', 'invalid']) {
    const h = adminHarness([record({review_status, prompt_version: 'answer-v5+adaptive-prompt'})]);
    await h.run('loadReviews()');
    const text = content(metadata(h.list().children[0]));
    assert.match(text, /提示词版本：answer-v5\+adaptive-prompt/);
    assert.doesNotMatch(text, /审核/);
  }
  const h = adminHarness([record({prompt_version: null})]);
  await h.run('loadReviews()');
  assert.match(content(metadata(h.list().children[0])), /提示词版本：—/);
});

test('zero duration is a measured value while missing or invalid durations remain unavailable', async () => {
  const h = adminHarness([record({task_completion_seconds: 0, dialogue_elapsed_seconds: null,
    decision_elapsed_seconds: -1})]);
  await h.run('loadReviews()');
  const text = content(metadata(h.list().children[0]) || h.list());
  assert.match(text, /任务总耗时：0\.0 秒/);
  assert.match(text, /对话阶段：—/);
  assert.match(text, /决策阶段：—/);
});

test('older API records do not silently claim membership of the current statistical cohort', async () => {
  const h = adminHarness([record({in_current_cohort: undefined, cohort_exclusion_reasons: undefined,
    run_mode: undefined, literacy_instrument_version: undefined, model_config_id: undefined,
    prompt_version: undefined})]);
  await h.run('loadReviews()');
  const text = content(h.list());
  assert.match(text, /统计范围未标注/);
  assert.doesNotMatch(text, /undefined|NaN/);
});

test('large review queues render fifty records and retain the page on refresh', async () => {
  const rows = Array.from({length: 126}, (_, i) => record({id: `SYN-${String(i + 1).padStart(4, '0')}`}));
  const h = adminHarness(rows);
  await h.run('loadReviews()');
  assert.equal(h.list().querySelectorAll('article').length, 50);
  assert.match(content(h.list().querySelector('nav')), /共 126 条.*第 1\/3 页.*1–50/);
  assert.equal(h.list().querySelector('nav').querySelectorAll('button')[0].disabled, true);
  await h.list().querySelector('nav').querySelectorAll('button')[1].emit('click');
  assert.equal(h.list().querySelector('article').querySelector('h3').textContent, '记录 R0051 · 已完成');
  await h.run('loadReviews()');
  assert.equal(h.list().querySelectorAll('article').length, 50);
  assert.match(content(h.list().querySelector('nav')), /第 2\/3 页.*51–100/);
  assert.equal(h.list().querySelector('article').querySelector('h3').textContent, '记录 R0051 · 已完成');
  rows.splice(20);
  await h.run('loadReviews()');
  assert.equal(h.list().querySelectorAll('article').length, 20);
  assert.equal(h.list().querySelector('nav'), null);
  assert.equal(h.list().querySelector('article').querySelector('h3').textContent, '记录 R0001 · 已完成');
});

test('neutral record IDs are consecutive across pages without exposing original session IDs', async () => {
  const rows = Array.from({length:126}, (_, index) => record({
    id:`SYN-${String(600 - index * 3).padStart(4,'0')}`,
    ended_at:new Date(Date.UTC(2030,0,1,0,index)).toISOString()}));
  const h = adminHarness(rows);
  h.run('configureAdminEnvironment({showcase_mode:true})');
  await h.run('loadReviews()');
  assert.equal(h.list().querySelector('article').querySelector('h3').textContent, '记录 R0001 · 已完成');
  assert.match(content(metadata(h.list().querySelector('article'))), /记录编号：R0001/);
  assert.doesNotMatch(content(h.list()), /SYN-/);
  rows.reverse();
  await h.run('loadReviews()');
  assert.equal(h.list().querySelector('article').querySelector('h3').textContent, '记录 R0001 · 已完成');
  assert.match(content(metadata(h.list().querySelector('article'))), /记录编号：R0001/);
  assert.doesNotMatch(content(h.list()), /SYN-/);
  await h.list().querySelector('nav').querySelectorAll('button')[1].emit('click');
  assert.equal(h.list().querySelector('article').querySelector('h3').textContent, '记录 R0051 · 已完成');
  await h.list().querySelector('nav').querySelectorAll('button')[1].emit('click');
  const lastPage = h.list().querySelectorAll('article');
  assert.equal(lastPage[0].querySelector('h3').textContent, '记录 R0101 · 已完成');
  assert.equal(lastPage.at(-1).querySelector('h3').textContent, '记录 R0126 · 已完成');
  await h.run('loadReviews()');
  assert.equal(h.list().querySelectorAll('article').at(-1).querySelector('h3').textContent, '记录 R0126 · 已完成');
  assert.equal(new Set(rows.map(row => row.id)).size,126);
});

test('equal submission timestamps have a stable ID tie-breaker without changing source records', async () => {
  const rows = [record({id:'SYN-0600'}),record({id:'SYN-0093'})];
  const h = adminHarness(rows);
  await h.run('loadReviews()');
  const cards = h.list().querySelectorAll('article');
  assert.equal(cards[0].querySelector('h3').textContent, '记录 R0001 · 已完成');
  assert.match(content(metadata(cards[0])), /记录编号：R0001/);
  assert.equal(cards[1].querySelector('h3').textContent, '记录 R0002 · 已完成');
  assert.match(content(metadata(cards[1])), /记录编号：R0002/);
  assert.doesNotMatch(content(h.list()), /SYN-/);
  assert.deepEqual(rows.map(row => row.id),['SYN-0600','SYN-0093']);
});

test('canonical record_id from the backend stays identical in the title and detail view', async () => {
  const rows = [record({id:'SYN-0600',record_id:'R0093'})];
  const h = adminHarness(rows);
  await h.run('loadReviews()');
  assert.equal(h.list().querySelector('article').querySelector('h3').textContent, '记录 R0093 · 已完成');
  assert.match(content(metadata(h.list().querySelector('article'))), /记录编号：R0093/);
  assert.doesNotMatch(content(h.list()), /SYN-|0600/);
  assert.equal(rows[0].id, 'SYN-0600');
  assert.equal(rows[0].record_id, 'R0093');
});

test('university showcase labels its synthetic record counts and retains ordinary export buttons', () => {
  const h = adminHarness([]);
  h.run('configureAdminEnvironment({showcase_mode:true,showcase_totals:{sessions:126,submitted:126,unsubmitted:0}})');
  assert.match(content(h.document.getElementById('admin-mode')), /大学生合成展示.*126 条记录.*已完成 126 条.*未完成 0 条/);
  assert.match(content(h.document.getElementById('admin-mode')), /大学生合成展示/);
  assert.equal(h.document.getElementById('synthetic-download-section').hidden, false);
  assert.equal(h.document.getElementById('analysis-export-link').textContent, '已完成记录 CSV');
  const ordinary = h.document.getElementById('data-export').querySelectorAll('a')
    .filter(link => (link.attributes.get('href') || '').startsWith('/api/admin/export?'));
  assert.equal(ordinary.length, 3);
  assert.match(h.document.getElementById('data-export-description').textContent, /完成整份信息素养测试并已提交任务/);
  assert.match(h.document.getElementById('data-export-description').textContent, /当前同一批记录全部完成时，两类导出的人数可以相同/);
  assert.match(h.document.getElementById('data-export-description').textContent, /AI 主动提示、澄清和引导次数/);
  assert.match(h.document.getElementById('data-export-description').textContent, /常用列包含记录编号、年龄、分组/);
  assert.match(h.document.getElementById('data-export-description').textContent, /完整对话单独下载，各下载使用同一记录编号/);
});

test('university showcase downloads only the current 126 batch and omits historical 600 data', async () => {
  const document = makeDocument();
  const requested = [];
  const datasets = [
    {design_version:'literacy-age-v5',available:true,n_total:600,n_completed:587,n_dropout:13,
      files:[{key:'calibrated_csv',label:'历史600数据',available:true}]},
    {design_version:'university-v1',available:true,n_total:126,n_completed:126,n_dropout:0,
      notice:'大学生虚构记录，不是实证结果。',files:[{key:'csv',label:'当前大学生CSV',available:true},
      {key:'materials_json',label:'共同问卷与任务',available:true}]},
  ];
  const context = vm.createContext({document, console, fetch: async url => {
    requested.push(url);
    return response(url === '/api/info' ? {showcase_mode:true} : {datasets});
  }});
  const source = fs.readFileSync(path.join(__dirname, '../static/admin-synthetic.js'), 'utf8');
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(document.getElementById('synthetic-download-section').hidden, false);
  const catalogue = document.getElementById('synthetic-datasets');
  assert.match(content(catalogue), /126条大学生合成记录/);
  assert.doesNotMatch(content(catalogue), /600|历史600数据/);
  const links = catalogue.querySelectorAll('a');
  assert.deepEqual(links.map(link => link.href), ['/api/admin/synthetic-export?file=csv', '/api/admin/synthetic-export?file=materials_json']);
  await document.getElementById('refresh-button').emit('click');
  assert.deepEqual(requested, ['/api/info','/api/admin/synthetic-datasets','/api/admin/synthetic-datasets']);
});

test('showcase comparison renders the three supplied groups and updates from new data', () => {
  const h = adminHarness([]);
  h.run('configureAdminEnvironment({showcase_mode:true})');
  h.run(`renderShowcaseComparison({groups:[
    {literacy_group:'高',standard_n:21,standard_mean:80,adaptive_n:21,adaptive_mean:95,difference_points:15},
    {literacy_group:'中',standard_n:21,standard_mean:70,adaptive_n:21,adaptive_mean:80,difference_points:10},
    {literacy_group:'低',standard_n:21,standard_mean:60,adaptive_n:21,adaptive_mean:63,difference_points:3}],
    gap_standard:20,gap_adaptive:32})`);
  const box = h.document.getElementById('showcase-comparison');
  assert.equal(box.hidden, false);
  const rows = box.querySelector('tbody').children;
  assert.deepEqual(rows.map(row => row.children.map(cell => cell.textContent)), [
    ['高素养组','21','80.0','21','95.0','15.0'],
    ['中素养组','21','70.0','21','80.0','10.0'],
    ['低素养组','21','60.0','21','63.0','3.0']]);
  assert.match(content(box), /无澄清 20\.0分 → 有澄清 32\.0分/);
  h.run(`renderShowcaseComparison({groups:[{literacy_group:'低',standard_n:1,standard_mean:20,
    adaptive_n:1,adaptive_mean:40,difference_points:20}],gap_standard:null,gap_adaptive:null})`);
  assert.equal(box.querySelectorAll('tbody').length, 1);
  assert.deepEqual(box.querySelector('tbody').children[2].children.map(cell => cell.textContent),
    ['低素养组','1','20.0','1','40.0','20.0']);
  assert.doesNotMatch(content(box), /95\.0|32\.0分/);
});

test('comparison leaves missing means unavailable and stays hidden outside showcase mode', () => {
  const h = adminHarness([]);
  h.run('renderShowcaseComparison({groups:[]})');
  const box = h.document.getElementById('showcase-comparison');
  assert.equal(box.hidden, true);
  assert.equal(box.children.length, 0);
  h.run(`configureAdminEnvironment({showcase_mode:true});renderShowcaseComparison({groups:[
    {literacy_group:'高',standard_n:0,standard_mean:null,adaptive_n:2,adaptive_mean:null,difference_points:null}],
    gap_standard:null,gap_adaptive:null})`);
  assert.deepEqual(box.querySelector('tbody').children[0].children.map(cell => cell.textContent),
    ['高素养组','0','—','2','—','—']);
  assert.deepEqual(box.querySelector('tbody').children[1].children.map(cell => cell.textContent),
    ['中素养组','—','—','—','—','—']);
  assert.doesNotMatch(content(box), /NaN|undefined|null|20\.0|32\.0/);
});

test('showcase review rows omit repeated mode labels while normal rows retain them', async () => {
  const h = adminHarness([record({run_mode:'demo'})]);
  h.run('configureAdminEnvironment({showcase_mode:true})');
  await h.run('loadReviews()');
  assert.doesNotMatch(content(h.list()), /DEMO|PILOT|LIVE/);
  h.run('configureAdminEnvironment({showcase_mode:false})');
  await h.run('loadReviews()');
  assert.match(content(h.list()), /DEMO/);
});

test('summary uses automatically included completed counts and never asks for review', async () => {
  const h = adminHarness([], async url => {
    assert.equal(url, '/api/admin/summary?age_band=18-20');
    return response({study_mode:'pilot', clarification_strategy:'module', active_task_title:'当前任务',
      groups:[{group_id:'S',sessions:70,submitted:66,completed_n:66,analysis_n:63,valid:9,valid_mean_score:80}],
      pending_reviews:100, strata:[],band_width:10,literacy_distribution:[],score_distribution:[],
      evidence_reveal_distribution:[],condition_comparison:{comparisons:[]},mean_reply_seconds:null});
  });
  h.document.getElementById('admin-age-band').value = '18-20';
  await h.run('loadSummary()');
  const text = content(h.document.getElementById('summary'));
  assert.match(text, /分配 70 · 完成 66 · 纳入统计 63/);
  assert.match(text, /完成样本平均分 80\.0/);
  assert.doesNotMatch(text, /审核|有效 9/);
});

test('dialogue exports use separate authenticated API links for the completed analysis sample', () => {
  const h = adminHarness([]);
  const links = h.document.getElementById('data-export').querySelectorAll('a')
    .map(link => link.attributes.get('href'))
    .filter(href => (href || '').startsWith('/api/admin/dialogue-export?'));
  assert.deepEqual(links, ['/api/admin/dialogue-export?format=csv', '/api/admin/dialogue-export?format=json']);
  const html = fs.readFileSync(path.join(__dirname, '../static/admin.html'), 'utf8');
  assert.match(html, /href="\/api\/admin\/export\?kind=raw&format=csv">记录 CSV</);
  assert.match(html, /href="\/api\/admin\/export\?kind=raw&format=json">记录 JSON</);
  assert.match(html, /返回测试/);
  assert.match(html, /信息素养测试作答/);
  assert.match(html, /记录与对话查看/);
  assert.doesNotMatch(html, /人工审核|待审核|审核有效|问卷预测试|预测试作答/);
  assert.doesNotMatch(shippedScript, /\/sessions\/\$\{row\.id\}\/(?:review|score)/);
});
