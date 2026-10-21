"""Administrator login, session isolation and authorization regressions."""
from dataclasses import replace
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.admin_auth import BROWSER_COOKIE, SESSION_COOKIE, SESSION_SECONDS, issue_session, valid_session
from app.main import create_app
from test_flow import auth, settings, start_payload


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(settings(tmp_path)))


def login(client, **kwargs):
    return client.post('/api/admin/login', json={
        'username': 'admin', 'password': 'test-password'}, **kwargs)


@pytest.mark.parametrize('path', ['/admin', '/admin/preview', '/admin/instruments'])
def test_protected_pages_send_visitors_to_login_without_exposing_content(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/admin/login?next=' + quote(path, safe='')
    assert 'no-store' in response.headers['cache-control']
    assert 'data-researcher-preview' not in response.text
    assert 'WWW-Authenticate' not in response.headers


def test_login_page_is_public_and_does_not_include_backend_data(client):
    response = client.get('/admin/login')
    assert response.status_code == 200
    assert '<form' in response.text and 'type="password"' in response.text
    assert 'test-password' not in response.text
    assert 'id="review-list"' not in response.text
    assert 'no-store' in response.headers['cache-control']


@pytest.mark.parametrize('path', [
    '/api/admin/summary', '/api/admin/tasks', '/api/admin/pretest-analysis',
    '/api/admin/review-queue', '/api/admin/instruments',
    '/api/admin/export?kind=raw&format=json',
    '/api/admin/archive-export?kind=raw&format=json',
])
def test_admin_data_stays_protected_and_basic_clients_keep_working(client, path):
    denied = client.get(path)
    assert denied.status_code == 401
    assert 'WWW-Authenticate' not in denied.headers
    assert client.get(path, headers=auth()).status_code == 200


@pytest.mark.parametrize('username,password', [
    ('admin', 'wrong-password'), ('other', 'test-password'),
    ('管理员', 'test-password'), ('admin', '错误密码'),
])
def test_bad_login_has_a_clear_error_and_never_creates_a_session(client, username, password):
    response = client.post('/api/admin/login', json={'username': username, 'password': password})
    assert response.status_code == 401
    assert response.json()['detail'] == '用户名或密码错误'
    assert 'set-cookie' not in response.headers
    assert SESSION_COOKIE not in client.cookies
    assert client.get('/api/admin/summary').status_code == 401


def test_login_cookie_opens_admin_pages_data_and_researcher_preview(client):
    response = login(client, headers={'Origin': 'http://testserver'})
    assert response.status_code == 200 and response.json() == {'ok': True}
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert 'path=/' in cookie and f'max-age={SESSION_SECONDS}' in cookie
    assert 'domain=' not in cookie and 'test-password' not in cookie
    marker = next(value for value in response.headers.get_list('set-cookie')
                  if value.startswith(BROWSER_COOKIE + '='))
    assert response.cookies.get(BROWSER_COOKIE) == '1'
    assert 'httponly' in marker.lower() and 'samesite=strict' in marker.lower()
    assert 'max-age=' not in marker.lower() and 'expires=' not in marker.lower()
    for path in ['/admin', '/admin/preview', '/admin/instruments',
                 '/api/admin/summary', '/api/admin/tasks',
                 '/api/admin/pretest-analysis', '/api/admin/review-queue',
                 '/api/admin/instruments', '/api/admin/export?kind=raw&format=json']:
        assert client.get(path).status_code == 200, path
    preview = client.post('/api/start', json={**start_payload(), 'researcher_preview': True},
                          headers={'Origin': 'http://testserver'})
    assert preview.status_code == 200
    assert client.get('/api/admin/summary').json()['groups'] == []


@pytest.mark.parametrize('headers', [
    {'Authorization': 'Basic invalid-base64'},
    {'Authorization': 'Basic YWRtaW46d3JvbmctcGFzc3dvcmQ='},
])
def test_stale_browser_basic_credentials_do_not_override_a_valid_cookie(client, headers):
    assert login(client).status_code == 200
    assert client.get('/admin', headers=headers).status_code == 200
    assert client.get('/api/admin/summary', headers=headers).status_code == 200


def test_https_login_uses_a_secure_cookie(tmp_path):
    client = TestClient(create_app(settings(tmp_path)), base_url='https://testserver')
    response = login(client, headers={'Origin': 'https://testserver'})
    assert response.status_code == 200
    assert '; secure' in response.headers['set-cookie'].lower()
    assert client.get('/api/admin/summary').status_code == 200


def test_https_proxy_login_matches_browser_origin_and_uses_secure_cookie(client):
    response = login(client, headers={
        'Host': 'study.example.test', 'X-Forwarded-Proto': 'https',
        'Origin': 'https://study.example.test',
    })
    assert response.status_code == 200
    assert '; secure' in response.headers['set-cookie'].lower()


@pytest.mark.parametrize('origin', [
    'https://unrelated.example', 'http://testserver.evil.example',
    'null', 'https://testserver', 'http://testserver:8080',
])
def test_cross_origin_login_is_rejected_even_with_correct_password(client, origin):
    response = login(client, headers={'Origin': origin})
    assert response.status_code == 403
    assert SESSION_COOKIE not in client.cookies


def test_cookie_session_rejects_cross_origin_changes_and_logout(client):
    assert login(client).status_code == 200
    headers = {'Origin': 'https://unrelated.example'}
    assert client.post('/api/admin/tasks/missing/active/1', headers=headers).status_code == 403
    assert client.post('/api/start', json={**start_payload(), 'researcher_preview': True},
                       headers=headers).status_code == 403
    assert client.post('/api/admin/logout', headers=headers).status_code == 403
    assert client.get('/api/admin/summary').status_code == 200
    response = client.post('/api/admin/logout', headers={'Origin': 'http://testserver'})
    assert response.status_code == 200 and response.json() == {'ok': True}
    assert 'max-age=0' in response.headers['set-cookie'].lower()
    assert SESSION_COOKIE not in client.cookies
    assert client.get('/api/admin/summary').status_code == 401


def test_cached_basic_cannot_sign_a_browser_back_in_after_logout(client):
    basic = auth()
    assert client.get('/api/admin/summary', headers=basic).status_code == 200
    assert login(client, headers=basic).status_code == 200
    assert client.get('/api/admin/summary', headers=basic).status_code == 200
    response = client.post('/api/admin/logout', headers=basic)
    assert response.status_code == 200
    assert SESSION_COOKIE not in client.cookies
    assert client.cookies.get(BROWSER_COOKIE) == '1'
    denied = client.get('/admin', headers=basic, follow_redirects=False)
    assert denied.status_code == 303 and denied.headers['location'] == '/admin/login?next=%2Fadmin'
    assert client.get('/api/admin/summary', headers=basic).status_code == 401
    assert login(client, headers=basic).status_code == 200
    assert client.get('/admin', headers=basic, follow_redirects=False).status_code == 200
    assert client.get('/api/admin/summary', headers=basic).status_code == 200


@pytest.mark.parametrize('name,value', [
    (BROWSER_COOKIE, '1'),
    (SESSION_COOKIE, 'invalid-session'),
])
def test_browser_marker_or_invalid_session_suppresses_cached_basic(client, name, value):
    client.cookies.set(name, value)
    assert client.get('/admin', headers=auth(), follow_redirects=False).status_code == 303
    assert client.get('/api/admin/summary', headers=auth()).status_code == 401
    assert login(client).status_code == 200
    assert client.get('/api/admin/summary', headers=auth()).status_code == 200


def test_basic_scripts_without_browser_cookies_still_access_the_backend(client):
    assert len(client.cookies) == 0
    assert client.get('/admin', headers=auth(), follow_redirects=False).status_code == 200
    assert client.get('/api/admin/summary', headers=auth()).status_code == 200
    assert len(client.cookies) == 0


def test_session_rejects_tampering_expiration_and_password_changes(client, tmp_path):
    token = issue_session('test-password', now=1000)
    assert valid_session(token, 'test-password', now=1000)
    assert valid_session(token, 'test-password', now=1000 + SESSION_SECONDS - 1)
    assert not valid_session(token, 'test-password', now=1000 + SESSION_SECONDS)
    assert not valid_session(token, 'new-password', now=1000)
    assert not valid_session(issue_session('test-password', now=2000), 'test-password', now=1000)
    tampered = token[:-1] + ('0' if token[-1] != '0' else '1')
    assert not valid_session(tampered, 'test-password', now=1000)

    expired = issue_session('test-password', now=time.time() - SESSION_SECONDS)
    client.cookies.set(SESSION_COOKIE, expired)
    assert client.get('/api/admin/summary').status_code == 401
    client.cookies.clear()
    response = login(client)
    assert response.status_code == 200
    old_cookie = response.cookies.get(SESSION_COOKIE)
    rotated = TestClient(create_app(replace(settings(tmp_path), admin_password='new-password')))
    rotated.cookies.set(SESSION_COOKIE, old_cookie)
    assert rotated.get('/api/admin/summary').status_code == 401


@pytest.mark.parametrize('token', [None, '', 'invalid', 'v1.1.a.b', '\u4e2d\u6587', 'x' * 8192])
def test_malformed_session_tokens_are_rejected(token):
    assert not valid_session(token, 'test-password')
