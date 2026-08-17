from fastapi.testclient import TestClient

from app.main import app


def test_login_required_for_index():
    client = TestClient(app)
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('/login')


def test_login_and_logout_flow(monkeypatch):
    monkeypatch.setenv('APP_AUTH_USERNAME', 'test-admin')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'test-password-for-smoke')
    monkeypatch.setenv('APP_SESSION_SECRET', 'test-session-secret-for-smoke')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    client = TestClient(create_app(), headers={'Origin': 'http://testserver'})

    response = client.post(
        '/login',
        data={'username': 'test-admin', 'password': 'test-password-for-smoke', 'next_path': '/'},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers['location'] == '/'

    response = client.post('/logout', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/login'

def test_state_changing_request_requires_same_origin(monkeypatch):
    monkeypatch.setenv('APP_AUTH_USERNAME', 'test-admin')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'test-password-for-smoke')
    monkeypatch.setenv('APP_SESSION_SECRET', 'test-session-secret-for-smoke')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    client = TestClient(create_app())
    response = client.post(
        '/login',
        data={'username': 'test-admin', 'password': 'test-password-for-smoke', 'next_path': '/'},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_login_rejects_external_next_path(monkeypatch):
    monkeypatch.setenv('APP_AUTH_USERNAME', 'test-admin')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'test-password-for-smoke')
    monkeypatch.setenv('APP_SESSION_SECRET', 'test-session-secret-for-smoke')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    client = TestClient(create_app(), headers={'Origin': 'http://testserver'})
    response = client.post(
        '/login',
        data={'username': 'test-admin', 'password': 'test-password-for-smoke', 'next_path': 'https://evil.example/'},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers['location'] == '/'

def test_https_only_session_cookie_sets_secure(monkeypatch):
    monkeypatch.setenv('APP_AUTH_USERNAME', 'test-admin')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'test-password-for-smoke')
    monkeypatch.setenv('APP_SESSION_SECRET', 'test-session-secret-for-smoke')
    monkeypatch.setenv('APP_SESSION_HTTPS_ONLY', 'true')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    client = TestClient(create_app(), headers={'Origin': 'http://testserver'})
    response = client.post(
        '/login',
        data={'username': 'test-admin', 'password': 'test-password-for-smoke', 'next_path': '/'},
        follow_redirects=False,
    )

    assert response.status_code == 303
    cookie = response.headers['set-cookie'].lower()
    assert 'secure' in cookie
    assert 'httponly' in cookie

def test_production_rejects_default_credentials(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'change-me')
    monkeypatch.setenv('APP_SESSION_SECRET', 'change-this-session-secret')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    try:
        create_app()
    except RuntimeError as exc:
        message = str(exc)
        assert 'APP_AUTH_PASSWORD' in message
        assert 'APP_SESSION_SECRET' in message
        assert 'change-me' not in message
        assert 'change-this-session-secret' not in message
    else:
        raise AssertionError('production startup should reject default credentials')


def test_production_rejects_short_session_secret(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'a-real-password-for-production')
    monkeypatch.setenv('APP_SESSION_SECRET', 'short-secret')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    try:
        create_app()
    except RuntimeError as exc:
        assert '长度不能少于 32' in str(exc)
    else:
        raise AssertionError('production startup should reject a short session secret')

def test_healthz_is_available_without_login(monkeypatch):
    monkeypatch.delenv('APP_ENV', raising=False)
    monkeypatch.delenv('APP_SESSION_HTTPS_ONLY', raising=False)
    monkeypatch.setenv('APP_SESSION_SECRET', 'development-health-secret-for-test')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    response = TestClient(create_app()).get('/healthz')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}

def test_production_rejects_multiple_workers(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'a-real-password-for-production')
    monkeypatch.setenv('APP_SESSION_SECRET', 'a-session-secret-that-is-at-least-32-characters-long')
    monkeypatch.setenv('APP_WORKERS', '2')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    try:
        create_app()
    except RuntimeError as exc:
        message = str(exc)
        assert '单实例单 Worker' in message
        assert '共享状态架构' in message
    else:
        raise AssertionError('production startup should reject multiple workers')

def test_login_failures_are_rate_limited(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('APP_AUTH_USERNAME', 'test-admin')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'test-password-for-smoke')
    monkeypatch.setenv('APP_SESSION_SECRET', 'test-session-secret-for-smoke')

    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    from app.routes import LOGIN_FAILURE_LIMITER

    LOGIN_FAILURE_LIMITER.clear()
    client = TestClient(create_app(), headers={'Origin': 'http://testserver'})
    responses = [
        client.post('/login', data={'username': 'wrong', 'password': 'wrong', 'next_path': '/'})
        for _ in range(6)
    ]

    assert [response.status_code for response in responses[:4]] == [401] * 4
    assert responses[4].status_code == 429
    assert responses[5].status_code == 429
    assert int(responses[4].headers['retry-after']) > 0
    LOGIN_FAILURE_LIMITER.clear()

def test_security_headers_and_production_hsts(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'a-real-password-for-production')
    monkeypatch.setenv('APP_SESSION_SECRET', 'a-session-secret-that-is-at-least-32-characters-long')
    monkeypatch.setenv('APP_SESSION_HTTPS_ONLY', 'true')

    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app

    response = TestClient(create_app()).get('/healthz')

    assert response.status_code == 200
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'strict-origin-when-cross-origin'
    assert response.headers['permissions-policy'] == 'camera=(), microphone=(), geolocation=()'
    assert response.headers['strict-transport-security'] == 'max-age=31536000; includeSubDomains'
    monkeypatch.setenv('APP_ENV', 'development')
    get_settings.cache_clear()
