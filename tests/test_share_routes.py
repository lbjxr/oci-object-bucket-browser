import json
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from app.share_store import get_share_store, reset_share_store


def share_client(tmp_path):
    from tests.test_upload_routes import make_client

    client, fake_storage, manager = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/report.pdf': b'pdf-data',
        'docs/readme.txt': b'readme-data',
    }
    return client, fake_storage, manager


def create_share(client, *, object_key='docs/report.pdf', password='', download_limit=None, expires_in_hours=24):
    response = client.post(
        '/api/shares',
        json={
            'object_key': object_key,
            'password': password,
            'download_limit': download_limit,
            'expires_in_hours': expires_in_hours,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    token = urlparse(payload['share_url']).path.rsplit('/', 1)[-1]
    return payload, token


def test_share_management_page_and_create_response_only_exposes_plain_token_once(tmp_path):
    client, _fake_storage, _manager = share_client(tmp_path)
    page = client.get('/shares')
    assert page.status_code == 200
    assert '创建分享' in page.text
    assert '/static/js/shares.js' in page.text

    payload, token = create_share(client, password='secure-share', download_limit=3)
    assert token
    assert payload['share']['password_protected'] is True
    assert payload['share']['object_type_label'] == 'PDF'
    assert payload['share']['remaining_downloads'] == 3
    assert payload['share_url'].endswith(f'/s/{token}')
    assert 'token_hash' not in payload['share']
    assert 'password_hash' not in payload['share']

    raw_text = (tmp_path / 'shares.json').read_text(encoding='utf-8')
    raw = json.loads(raw_text)
    assert token not in raw_text
    assert 'secure-share' not in raw_text
    assert raw['shares'][0]['object_key'] == 'docs/report.pdf'

    listing = client.get('/api/shares')
    assert listing.status_code == 200
    assert listing.json()['summary']['active_count'] == 1
    assert listing.json()['shares'][0]['password_protected'] is True
    assert 'token_hash' not in listing.text
    assert 'password_hash' not in listing.text

    export = client.get('/api/shares-export')
    assert export.status_code == 200
    assert '对象路径' in export.content.decode('utf-8-sig')
    assert 'docs/report.pdf' in export.content.decode('utf-8-sig')


def test_public_password_flow_and_download_limit(tmp_path):
    client, _fake_storage, _manager = share_client(tmp_path)
    _payload, token = create_share(client, password='secure-share', download_limit=1)

    landing = client.get(f'/s/{token}')
    assert landing.status_code == 200
    assert '访问密码' in landing.text
    assert 'docs/report.pdf' in landing.text

    blocked_download = client.get(f'/s/{token}/download', follow_redirects=False)
    assert blocked_download.status_code == 303
    assert blocked_download.headers['location'] == f'/s/{token}'

    wrong = client.post(f'/s/{token}/verify-password', data={'password': 'wrong'}, follow_redirects=False)
    assert wrong.status_code == 401
    assert '分享密码错误' in wrong.text

    verified = client.post(
        f'/s/{token}/verify-password',
        data={'password': 'secure-share'},
        follow_redirects=False,
    )
    assert verified.status_code == 303
    assert verified.headers['location'] == f'/s/{token}'

    download = client.get(f'/s/{token}/download')
    assert download.status_code == 200
    assert download.content == b'pdf-data'
    assert download.headers['content-disposition'].startswith('attachment;')

    exhausted_landing = client.get(f'/s/{token}')
    assert exhausted_landing.status_code == 410
    assert '下载次数已用完' in exhausted_landing.text
    exhausted_download = client.get(f'/s/{token}/download')
    assert exhausted_download.status_code == 410


def test_public_unprotected_share_downloads_and_counts_access(tmp_path):
    client, _fake_storage, _manager = share_client(tmp_path)
    _payload, token = create_share(client, object_key='docs/readme.txt', download_limit=2)

    first_landing = client.get(f'/s/{token}')
    assert first_landing.status_code == 200
    assert '下载文件' in first_landing.text
    first = client.get(f'/s/{token}/download')
    second = client.get(f'/s/{token}/download')
    assert first.content == b'readme-data'
    assert second.content == b'readme-data'

    listing = client.get('/api/shares').json()
    share = listing['shares'][0]
    assert share['access_count'] == 1
    assert share['download_count'] == 2
    assert share['remaining_downloads'] == 0


def test_revoke_and_expiry_are_enforced_on_public_routes(tmp_path):
    client, _fake_storage, _manager = share_client(tmp_path)
    _payload, token = create_share(client)
    listing = client.get('/api/shares').json()['shares']
    share_id = listing[0]['id']

    revoked = client.delete(f'/api/shares/{share_id}')
    assert revoked.status_code == 200
    assert revoked.json()['share']['status'] == 'revoked'
    assert client.get(f'/s/{token}').status_code == 410
    assert client.get(f'/s/{token}/download').status_code == 410

    now = datetime.now(timezone.utc)
    expired_record, expired_token = get_share_store().create(
        object_key='docs/report.pdf',
        expires_at=now - timedelta(seconds=1),
        download_limit=None,
        now=now - timedelta(hours=1),
    )
    assert expired_record['id']
    assert client.get(f'/s/{expired_token}').status_code == 410
    assert '已过期' in client.get(f'/s/{expired_token}').text

    reset_share_store()


def test_share_api_requires_admin_login_but_public_route_does_not(tmp_path):
    client, _fake_storage, _manager = share_client(tmp_path)
    _payload, token = create_share(client)
    client.cookies.clear()

    assert client.get('/api/shares').status_code == 401
    assert client.get(f'/s/{token}').status_code == 200

def test_share_password_failures_are_rate_limited(tmp_path):
    client, _fake_storage, _manager = share_client(tmp_path)
    _payload, token = create_share(client, password='secure-share')

    responses = [
        client.post(f'/s/{token}/verify-password', data={'password': 'wrong-password'})
        for _ in range(6)
    ]
    assert [response.status_code for response in responses[:4]] == [401] * 4
    assert responses[4].status_code == 429
    assert responses[5].status_code == 429
    assert int(responses[4].headers['retry-after']) > 0
    assert int(responses[5].headers['retry-after']) > 0
    from app.routes import SHARE_FAILURE_LIMITER
    key = f"share:testclient:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"
    SHARE_FAILURE_LIMITER.reset(key)
