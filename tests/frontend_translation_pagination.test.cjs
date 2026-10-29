// Run actual production listeners against a browser-like DOM and sessionStorage.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const root = path.resolve(__dirname, '..');
const fixture = fs.readFileSync(path.join(__dirname, 'frontend_state.test.cjs'), 'utf8');
const fixtureClasses = fixture.slice(fixture.indexOf('class Storage'), fixture.indexOf('\nconst info ='));
const html = fs.readFileSync(path.join(root, 'static/translation.html'), 'utf8');
const storageKey = 'lens-translation-v1:participant:';
const info = {study_mode: 'pilot', ready: true, access_code_required: false};
const form = {version: 'pagination-fixture', items: Array.from({length: 18}, (_, i) => ({id:'q'+(i+1),stem:'题目'+(i+1),options:['选项零','选项一']})), self_items: Array.from({length:6},(_,i)=>({id:'s'+(i+1),stem:'自评'+(i+1),options:['自评零','自评一']}))};
function harness({stored = {}, currentInfo = info} = {}) {
  const {Storage, Element, makeDocument} = new Function('html', `${fixtureClasses}\nreturn {Storage, Element, makeDocument};`)(html);
  // The shared legacy fixture ignores CSS class selectors. Model those selectors
  // here instead of changing production code to accommodate a test-only DOM.
  Element.prototype.querySelectorAll = function(selector) {
    const found = [], selectors = selector.split(',').map(value => value.trim().split(/\s+/));
    const match = (node, simple) => {
      const tag = simple.match(/^[\w-]+/)?.[0]; if (tag && node.tagName !== tag) return false;
      if (simple.includes(':checked') && !node.checked) return false;
      if (simple.includes(':disabled') && !node.disabled) return false;
      const id = simple.match(/#([\w-]+)/)?.[1]; if (id && node.id !== id) return false;
      for (const [,name] of simple.matchAll(/\.([\w-]+)/g)) if (!String(node.className || '').split(/\s+/).includes(name) && !node.classList.contains(name)) return false;
      for (const [,key,value] of simple.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
        const actual = node[key] ?? node.attributes.get(key); if (value === undefined ? actual === undefined : String(actual) !== value) return false;
      }
      return true;
    };
    const visit = node => { for (const child of node.children) { if (selectors.some(parts => {
      if (!match(child,parts.at(-1))) return false;
      let ancestor=child.parentElement; for(let index=parts.length-2; index>=0; index--) { while(ancestor && !match(ancestor,parts[index])) ancestor=ancestor.parentElement; if(!ancestor)return false; ancestor=ancestor.parentElement; } return true;
    })) found.push(child); visit(child); } }; visit(this); return found;
  };
  Element.prototype.focus = function(options) { this.focusCount=(this.focusCount || 0)+1; this.focusOptions=options; };
  Element.prototype.scrollIntoView = function(options) { this.scrollCount=(this.scrollCount || 0)+1; this.scrollOptions=options; };
  const document = makeDocument(); document.body = document.querySelector('body');
  const storage = new Storage(stored), requests = [], windowEvents = new Map();
  const context = vm.createContext({document, sessionStorage: storage, Node: Element, crypto: webcrypto, AbortController, Date, Intl,
    navigator: {clipboard: {writeText: async () => {}}}, window: {confirm: () => true, addEventListener(name,callback) { windowEvents.set(name,callback); }}, location: {pathname: '/', href: ''},
    setInterval: () => 1, setTimeout: () => 1, clearTimeout() {}, async fetch(url) {
      requests.push(url); const body = url === '/api/translation/info' ? currentInfo : url === '/api/translation/form' ? form : null;
      if (!body) throw new Error('Pagination must not request a model or create a session');
      return {ok:true,status:200,json:async()=>structuredClone(body)};
    }});
  const source = fs.readFileSync(path.join(root, 'static/translation.js'), 'utf8').replace('  init();', `
    info = ${JSON.stringify(currentInfo)}; form = ${JSON.stringify(form)}; globalThis.paginationHooks = {init,start};`);
  vm.runInContext(source, context);
  const element = id => document.getElementById(id);
  for (const [id,value] of [['age-band','19'],['education-stage','year-2'],['translation-experience','3']]) element(id).value=value;
  element('university-confirmed').checked=true; element('consent').checked=true;
  return {element,storage,requests,windowEvents,init:context.paginationHooks.init,start:context.paginationHooks.start};
}
async function choose(field,value=0,emit=true) { const inputs=field.querySelectorAll('input'); for(const input of inputs)input.checked=false; const input=inputs[value];input.checked=true;if(emit)await input.emit('change'); }
async function answerPage(h,{emit=true,value=0}={}) { for(const field of h.element('form-items').children)await choose(field,value,emit); }
const snapshot = h => Object.fromEntries(h.storage.values);
const fields = h => h.element('form-items').children;

test('all five answers including option zero advance through click and survive back navigation',async()=>{
  const h=harness();await h.element('begin-form').emit('click');await answerPage(h);
  assert.equal(h.element('form-progress').textContent,'5 / 24 已作答');assert.match(h.element('form-page-status').textContent,/本页已答 5 \/ 5/);
  await h.element('form-next').emit('click');assert.equal(h.element('form-page').textContent,'第 2 / 5 页');assert.equal(fields(h)[0].dataset.questionId,'q6');
  await h.element('form-prev').emit('click');assert.ok(fields(h).every(field=>field.querySelectorAll('input')[0].checked));
  assert.deepEqual(JSON.parse(h.storage.getItem(storageKey)).answers,{q1:0,q2:0,q3:0,q4:0,q5:0});
});
test('checked DOM choices without change events are synchronized and saved before next',async()=>{
  const h=harness();await h.element('begin-form').emit('click');await answerPage(h,{emit:false});
  assert.equal(h.element('form-progress').textContent,'0 / 24 已作答');await h.element('form-next').emit('click');
  assert.equal(h.element('form-page').textContent,'第 2 / 5 页');assert.deepEqual(JSON.parse(h.storage.getItem(storageKey)).answers,{q1:0,q2:0,q3:0,q4:0,q5:0});
  assert.equal(h.requests.length,0);
});
test('missing answers give exact question numbers, an inline message and a visible focus target',async()=>{
  const h=harness();await h.element('begin-form').emit('click');await choose(fields(h)[1]);await choose(fields(h)[3]);await choose(fields(h)[4]);
  await h.element('form-next').emit('click');assert.equal(h.element('form-page').textContent,'第 1 / 5 页');
  assert.match(h.element('form-error').textContent,/第 1、3 题/);assert.match(h.element('form-page-status').textContent,/3 \/ 5/);
  assert.equal(fields(h)[0].classList.contains('unanswered'),true);assert.equal(fields(h)[0].querySelector('.question-error').hidden,false);
  assert.match(fields(h)[0].querySelector('.question-error').textContent,/第 1 题尚未作答/);assert.equal(fields(h)[0].scrollCount,1);assert.equal(fields(h)[0].querySelector('input').focusCount,1);
  await choose(fields(h)[0]);await choose(fields(h)[2]);assert.equal(h.element('form-error').textContent,'');await h.element('form-next').emit('click');assert.equal(h.element('form-page').textContent,'第 2 / 5 页');
});
test('rapid repeated clicks cannot skip the next unanswered page or leave page bounds',async()=>{
  const h=harness();await h.element('begin-form').emit('click');await h.element('form-prev').emit('click');assert.equal(h.element('form-page').textContent,'第 1 / 5 页');await answerPage(h);
  await Promise.all([h.element('form-next').emit('click'),h.element('form-next').emit('click')]);
  assert.equal(h.element('form-page').textContent,'第 2 / 5 页');assert.match(h.element('form-error').textContent,/第 6、7、8、9、10 题/);
});
test('reload restores the current questionnaire page and choices without creating a session',async()=>{
  const h=harness();await h.element('begin-form').emit('click');await answerPage(h);await h.element('form-next').emit('click');await answerPage(h);await h.element('form-next').emit('click');
  const reloaded=harness({stored:snapshot(h)});await reloaded.init();assert.equal(reloaded.element('form-panel').hidden,false);assert.equal(reloaded.element('form-page').textContent,'第 3 / 5 页');assert.equal(reloaded.element('form-progress').textContent,'10 / 24 已作答');
  assert.deepEqual(reloaded.requests,['/api/translation/info','/api/translation/form']);assert.equal(JSON.parse(reloaded.storage.getItem(storageKey)).sessionId,undefined);
});
test('legacy saved answers without page metadata resume at the first unanswered question',async()=>{
  const state={formVersion:form.version,intake:{age_band:'18-20',education_stage:'bachelor',translation_experience:'3'},answers:Object.fromEntries(Array.from({length:10},(_,i)=>['q'+(i+1),0])),selfAnswers:{}};
  const h=harness({stored:{[storageKey]:JSON.stringify(state)}});await h.init();assert.equal(h.element('form-panel').hidden,false);assert.equal(h.element('form-page').textContent,'第 3 / 5 页');assert.equal(h.element('form-progress').textContent,'10 / 24 已作答');
  assert.equal(h.element('consent').checked,false);assert.equal(h.element('university-confirmed').checked,false);assert.equal(h.requests.length,2);
  assert.equal(h.element('age-band').value,'');assert.equal(h.element('education-stage').value,'');
});
test('current undergraduate intake and its confirmations restore with questionnaire answers',async()=>{
  const state={stage:'form',formPage:1,formVersion:form.version,intake:{age_band:'20',education_stage:'bachelor',undergraduate_year:'year-3',eligibility_version:'cumt-undergraduate-v1',translation_experience:'3',consent:true,university_confirmed:true},answers:{q1:0,q2:0,q3:0,q4:0,q5:0},selfAnswers:{}};
  const h=harness({stored:{[storageKey]:JSON.stringify(state)}});await h.init();
  assert.equal(h.element('form-page').textContent,'第 2 / 5 页');assert.equal(h.element('form-progress').textContent,'5 / 24 已作答');
  assert.equal(h.element('age-band').value,'20');assert.equal(h.element('education-stage').value,'year-3');assert.equal(h.element('university-confirmed').checked,true);assert.equal(h.element('consent').checked,true);
});
test('final-page review locates an earlier missing answer by stable question id',async()=>{
  const answers=Object.fromEntries(Array.from({length:18},(_,i)=>['q'+(i+1),0]));delete answers.q7;
  const state={stage:'form',formPage:4,formVersion:form.version,answers,selfAnswers:Object.fromEntries(Array.from({length:6},(_,i)=>['s'+(i+1),0])),intake:{age_band:'18-20',education_stage:'bachelor',translation_experience:'3',consent:true,university_confirmed:true}};
  const h=harness({stored:{[storageKey]:JSON.stringify(state)}});await h.init();assert.equal(h.element('form-page').textContent,'第 5 / 5 页');await h.element('start-task').emit('click');
  assert.equal(h.element('form-page').textContent,'第 2 / 5 页');assert.match(h.element('form-error').textContent,/第 7 题/);assert.equal(fields(h)[1].dataset.questionId,'q7');assert.equal(fields(h)[1].classList.contains('unanswered'),true);assert.equal(h.requests.length,2);
});
test('invitation is never persisted when questionnaire stage and confirmations are saved',async()=>{
  const h=harness({currentInfo:{...info,access_code_required:true}});h.element('access-code').value='PRIVATE-PAGINATION-INVITE';await h.element('begin-form').emit('click');await answerPage(h);await h.element('form-next').emit('click');
  const raw=h.storage.getItem(storageKey);assert.ok(!raw.includes('PRIVATE-PAGINATION-INVITE'));const state=JSON.parse(raw);assert.equal(state.stage,'form');assert.equal(state.formPage,1);assert.equal(state.intake.consent,true);
});
test('navigation buttons are explicit non-submit controls and shipped assets have a new cache version',()=>{
  for(const id of ['form-prev','form-next','start-task'])assert.match(html,new RegExp(`<button id="${id}" type="button"`));
  assert.match(html,/translation\.js\?v=json-recovery-20261030/);assert.match(html,/translation\.css\?v=hitbox-20261030/);
});


test('a failed recent call does not erase the fact that the API previously connected',async()=>{
  const h=harness({currentInfo:{...info,model_api_verified:false,model_api_verified_at:'2026-10-29T16:59:36+00:00'}});await h.init();
  assert.match(h.element('mode-banner').textContent,/此前已连通模型 API/);
  assert.match(h.element('mode-banner').textContent,/最近一次调用未成功/);
  assert.doesNotMatch(h.element('mode-banner').textContent,/尚未记录成功/);
});
