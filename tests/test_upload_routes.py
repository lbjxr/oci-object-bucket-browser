from pathlib import Path

from fastapi.testclient import TestClient


def make_client(tmp_path: Path):
    import app.routes as routes
    from app.config import get_settings
    from app.main import create_app

    import os
    os.environ['APP_AUTH_USERNAME'] = 'test-admin'
    os.environ['APP_AUTH_PASSWORD'] = 'test-password-for-smoke'
    os.environ['APP_SESSION_SECRET'] = 'test-session-secret-for-smoke'
    os.environ['APP_UPLOAD_SESSION_DIR'] = str(tmp_path / 'upload-sessions')
    os.environ['APP_UPLOAD_CHUNK_SIZE_MB'] = '8'
    os.environ['APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB'] = '4'
    os.environ['APP_UPLOAD_PARALLELISM'] = '3'
    get_settings.cache_clear()

    class FakeStorage:
        def __init__(self):
            self.single_uploads = []
            self.multipart_created = []
            self.parts = {}
            self.commits = []
            self.aborts = []
            self.upload_part_calls = []
            self.fail_parts = {}

        def upload_file(self, object_name, fileobj, content_type=None):
            self.single_uploads.append((object_name, fileobj.read(), content_type))

        def create_multipart_upload(self, object_name, content_type=None):
            upload_id = f'mp-{len(self.multipart_created)+1}'
            self.multipart_created.append((object_name, content_type, upload_id))
            return upload_id

        def upload_part(self, *, object_name, multipart_upload_id, part_num, payload, content_type=None):
            self.upload_part_calls.append((multipart_upload_id, part_num, len(payload)))
            remaining_failures = self.fail_parts.get(part_num, 0)
            if remaining_failures > 0:
                self.fail_parts[part_num] = remaining_failures - 1
                raise RuntimeError(f'boom-part-{part_num}')
            self.parts[(multipart_upload_id, part_num)] = payload
            return f'etag-{part_num}'

        def commit_multipart_upload(self, *, object_name, multipart_upload_id, parts):
            self.commits.append((object_name, multipart_upload_id, parts))

        def abort_multipart_upload(self, *, object_name, multipart_upload_id):
            self.aborts.append((object_name, multipart_upload_id))

        def list_objects(self, prefix=''):
            return []

        def open_stream(self, object_name):
            raise AssertionError('not used in this test')

        def get_preview(self, object_name):
            raise AssertionError('not used in this test')

    fake_storage = FakeStorage()
    routes.get_storage = lambda: fake_storage
    client = TestClient(create_app())
    client.post('/login', data={'username': 'test-admin', 'password': 'test-password-for-smoke', 'next_path': '/'})
    return client, fake_storage


def test_small_file_upload_still_uses_single_put(tmp_path):
    client, fake_storage = make_client(tmp_path)
    response = client.post(
        '/upload',
        files={'file': ('tiny.txt', b'hello world', 'text/plain')},
        headers={'X-Requested-With': 'XMLHttpRequest'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['strategy'] == 'single-put'
    assert fake_storage.single_uploads[0][0] == 'tiny.txt'


def test_init_upload_session_returns_parallelism_and_strategy(tmp_path):
    client, _ = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'init-only',
        },
    )
    assert init.status_code == 200
    payload = init.json()
    assert payload['ok'] is True
    assert payload['reused'] is False
    assert payload['strategy'] == 'oci-multipart-browser-chunked'
    assert payload['parallelism'] == 3
    assert payload['uploaded_parts'] == []
    assert payload['uploaded_bytes'] == 0



def test_multipart_flow_supports_resume_and_complete(tmp_path):
    client, fake_storage = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'abc123',
        },
    )
    assert init.status_code == 200
    init_payload = init.json()
    assert init_payload['strategy'] == 'oci-multipart-browser-chunked'
    assert init_payload['parallelism'] == 3
    upload_id = init_payload['upload_id']

    part1 = client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert part1.status_code == 200
    assert part1.json()['etag'] == 'etag-1'

    resumed = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'abc123',
        },
    )
    resumed_payload = resumed.json()
    assert resumed_payload['reused'] is True
    assert resumed_payload['upload_id'] == upload_id
    assert resumed_payload['uploaded_parts'] == [1]

    client.put(
        f'/api/uploads/{upload_id}/part/2',
        content=b'b' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    client.put(
        f'/api/uploads/{upload_id}/part/3',
        content=b'c' * (4 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    complete = client.post(f'/api/uploads/{upload_id}/complete')
    assert complete.status_code == 200
    assert fake_storage.commits
    object_name, multipart_id, parts = fake_storage.commits[0]
    assert object_name == 'big.bin'
    assert multipart_id == 'mp-1'
    assert parts == [(1, 'etag-1'), (2, 'etag-2'), (3, 'etag-3')]


def test_upload_part_returns_existing_etag_when_part_already_uploaded(tmp_path):
    client, fake_storage = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'dedupe-part',
        },
    )
    upload_id = init.json()['upload_id']

    first = client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert first.status_code == 200
    assert first.json()['already_uploaded'] is False

    second = client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert second.status_code == 200
    assert second.json()['already_uploaded'] is True
    assert fake_storage.upload_part_calls == [('mp-1', 1, 8 * 1024 * 1024)]



def test_cancel_upload_aborts_multipart(tmp_path):
    client, fake_storage = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 12 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'cancel-me',
        },
    )
    upload_id = init.json()['upload_id']
    response = client.delete(f'/api/uploads/{upload_id}')
    assert response.status_code == 200
    assert response.json()['message'] == '上传会话已取消'
    assert fake_storage.aborts == [('big.bin', 'mp-1')]

    status_after_cancel = client.get(f'/api/uploads/{upload_id}')
    assert status_after_cancel.status_code == 404



def test_complete_fails_when_parts_are_missing(tmp_path):
    client, _ = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'missing-parts',
        },
    )
    upload_id = init.json()['upload_id']

    client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    response = client.post(f'/api/uploads/{upload_id}/complete')
    assert response.status_code == 400
    assert '仍有分片未上传完成' in response.json()['detail']



def test_upload_part_failure_is_reported_without_mutating_uploaded_parts(tmp_path):
    client, fake_storage = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'failing-part',
        },
    )
    upload_id = init.json()['upload_id']
    fake_storage.fail_parts[2] = 1

    response = client.put(
        f'/api/uploads/{upload_id}/part/2',
        content=b'b' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert response.status_code == 500
    assert 'boom-part-2' in response.json()['detail']

    status = client.get(f'/api/uploads/{upload_id}')
    assert status.status_code == 200
    assert status.json()['uploaded_parts'] == []
