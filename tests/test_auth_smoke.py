from fastapi.testclient import TestClient

from app.main import app


def test_login_required_for_index():
    client = TestClient(app)
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('/login')


def test_login_and_logout_flow(monkeypatch):
    monkeypatch.setenv('APP_AUTH_USERNAME', 'admin')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'secret123')
    monkeypatch.setenv('APP_SESSION_SECRET', 'test-session-secret')

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    client = TestClient(create_app())

    response = client.post(
        '/login',
        data={'username': 'admin', 'password': 'secret123', 'next_path': '/'},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers['location'] == '/'

    response = client.post('/logout', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/login'
