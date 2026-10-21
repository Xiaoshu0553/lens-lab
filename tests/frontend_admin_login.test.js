// Run with: node --test tests/frontend_admin_login.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/admin-login.js'), 'utf8');

function loginHarness(query, fetchImpl) {
  const ids = ['admin-login-form', 'login-button', 'login-error', 'login-status', 'admin-username', 'admin-password'];
  const elements = Object.fromEntries(ids.map((id) => [id, {
    value: id === 'admin-username' ? 'admin' : id === 'admin-password' ? 'test-password' : '',
    textContent: '', disabled: false,
    addEventListener(type, callback) { this.callback = callback; }
  }]));
  const destinations = [];
  const context = vm.createContext({
    document: {getElementById: (id) => elements[id]},
    URLSearchParams, Set, TypeError, Error, JSON, fetch: fetchImpl,
    window: {location: {search: query, replace: (url) => destinations.push(url)}}
  });
  vm.runInContext(source, context, {filename: 'static/admin-login.js'});
  return {elements, destinations,
    submit: () => elements['admin-login-form'].callback({preventDefault() {}})};
}

test('login sends JSON credentials and opens each permitted administrator destination', async () => {
  for (const destination of ['/admin', '/admin/preview', '/admin/instruments']) {
    let calls = 0;
    const h = loginHarness('?next=' + encodeURIComponent(destination), async (url, options) => {
      calls++;
      assert.equal(url, '/api/admin/login');
      assert.equal(options.method, 'POST');
      assert.equal(options.credentials, 'same-origin');
      assert.equal(options.headers['Content-Type'], 'application/json');
      assert.deepEqual(JSON.parse(options.body), {username: 'admin', password: 'test-password'});
      assert.equal(h.elements['login-button'].disabled, true);
      return {ok: true, json: async () => ({ok: true})};
    });
    await h.submit();
    assert.deepEqual(h.destinations, [destination]);
    assert.equal(h.elements['admin-password'].value, '');
    assert.equal(h.elements['login-button'].disabled, false);
    assert.equal(calls, 1);
  }
});

test('missing and unsafe return destinations fall back to the administrator dashboard', async () => {
  for (const destination of ['', 'https://evil.invalid', '//evil.invalid', '/admin/../api/info',
    '/admin?next=https://evil.invalid', '/admin/login', '/admin\\evil.invalid']) {
    const h = loginHarness('?next=' + encodeURIComponent(destination),
      async () => ({ok: true, json: async () => ({ok: true})}));
    await h.submit();
    assert.deepEqual(h.destinations, ['/admin']);
  }
});

test('expired login notice and wrong credentials allow a corrected retry', async () => {
  let calls = 0;
  const h = loginHarness('?expired=1', async () => {
    calls++;
    return calls === 1
      ? {ok: false, status: 401, json: async () => ({detail: '用户名或密码错误'})}
      : {ok: true, json: async () => ({ok: true})};
  });
  assert.match(h.elements['login-status'].textContent, /登录已过期/);
  await h.submit();
  assert.deepEqual(h.destinations, []);
  assert.equal(h.elements['login-error'].textContent, '用户名或密码错误');
  assert.equal(h.elements['login-button'].disabled, false);
  await h.submit();
  assert.deepEqual(h.destinations, ['/admin']);
  assert.equal(h.elements['login-error'].textContent, '');
});

test('network failure displays a useful error and restores the submit button', async () => {
  const h = loginHarness('', async () => { throw new TypeError('Failed to fetch'); });
  await h.submit();
  assert.match(h.elements['login-error'].textContent, /网络连接失败/);
  assert.deepEqual(h.destinations, []);
  assert.equal(h.elements['login-button'].disabled, false);
  assert.equal(h.elements['login-button'].textContent, '登录');
});

test('repeated submissions share one pending request and a failed request can be retried', async () => {
  let calls = 0;
  let finish;
  const h = loginHarness('', async () => {
    calls++;
    return new Promise((resolve) => { finish = resolve; });
  });
  const first = h.submit();
  await h.submit();
  assert.equal(calls, 1);
  finish({ok: false, status: 401, json: async () => ({detail: '用户名或密码错误'})});
  await first;
  assert.equal(h.elements['login-button'].disabled, false);
  const retry = h.submit();
  assert.equal(calls, 2);
  finish({ok: true, json: async () => ({ok: true})});
  await retry;
  assert.deepEqual(h.destinations, ['/admin']);
});
