from pathlib import Path

from fastapi.testclient import TestClient
from oci.exceptions import ServiceError


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
            self.fail_part_errors = {}
            self.deleted_objects = []

        def upload_file(self, object_name, fileobj, content_type=None):
            self.single_uploads.append((object_name, fileobj.read(), content_type))

        def create_multipart_upload(self, object_name, content_type=None):
            upload_id = f'mp-{len(self.multipart_created)+1}'
            self.multipart_created.append((object_name, content_type, upload_id))
            return upload_id

        def upload_part(self, *, object_name, multipart_upload_id, part_num, payload, content_type=None):
            self.upload_part_calls.append((multipart_upload_id, part_num, len(payload)))
            error_factory = self.fail_part_errors.get(part_num)
            if callable(error_factory):
                raise error_factory()
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

        def delete_object(self, object_name):
            if callable(getattr(self, 'delete_hook', None)):
                return self.delete_hook(object_name)
            self.deleted_objects.append(object_name)

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
    payload = response.json()
    assert 'boom-part-2' in payload['detail']
    assert payload['retryable'] is False
    assert payload['error_code'] == 'unknown'

    status = client.get(f'/api/uploads/{upload_id}')
    assert status.status_code == 200
    assert status.json()['uploaded_parts'] == []


def test_upload_part_timeout_is_marked_retryable(tmp_path):
    client, fake_storage = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'timeout-part',
        },
    )
    upload_id = init.json()['upload_id']
    fake_storage.fail_part_errors[1] = lambda: TimeoutError('socket timed out')

    response = client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert response.status_code == 504
    payload = response.json()
    assert payload['retryable'] is True
    assert payload['error_code'] == 'timeout'
    assert '超时' in payload['reason']


def test_upload_part_http_5xx_is_marked_retryable(tmp_path):
    client, fake_storage = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'http-5xx-part',
        },
    )
    upload_id = init.json()['upload_id']
    fake_storage.fail_part_errors[2] = lambda: ServiceError(
        status=502,
        code='BadGateway',
        headers={},
        message='upstream unstable',
        request_endpoint='/uploadPart',
        client_version='test',
        timestamp='now',
        opc_request_id='req-1',
    )

    response = client.put(
        f'/api/uploads/{upload_id}/part/2',
        content=b'b' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert response.status_code == 503
    payload = response.json()
    assert payload['retryable'] is True
    assert payload['error_code'] == 'http_5xx'
    assert 'HTTP 502' in payload['reason']


def test_upload_part_http_4xx_stops_retry_early(tmp_path):
    client, fake_storage = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'http-4xx-part',
        },
    )
    upload_id = init.json()['upload_id']
    fake_storage.fail_part_errors[3] = lambda: ServiceError(
        status=400,
        code='InvalidParameter',
        headers={},
        message='bad request',
        request_endpoint='/uploadPart',
        client_version='test',
        timestamp='now',
        opc_request_id='req-2',
    )

    response = client.put(
        f'/api/uploads/{upload_id}/part/3',
        content=b'c' * (4 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload['retryable'] is False
    assert payload['error_code'] == 'http_4xx'
    assert 'HTTP 400' in payload['reason']


def test_delete_object_requires_login(tmp_path):
    client, fake_storage = make_client(tmp_path)
    client.cookies.clear()
    response = client.delete('/objects/sample.txt')
    assert response.status_code == 401
    assert response.json()['detail'] == '未登录'
    assert fake_storage.deleted_objects == []


def test_delete_object_success(tmp_path):
    client, fake_storage = make_client(tmp_path)
    response = client.delete('/objects/folder%2Fsample.txt')
    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['object_name'] == 'folder/sample.txt'
    assert payload['message'] == '已删除对象：folder/sample.txt'
    assert payload['detail'] == '对象“folder/sample.txt”已从 bucket 中移除。'
    assert fake_storage.deleted_objects == ['folder/sample.txt']


def test_delete_object_failure_is_reported(tmp_path):
    client, fake_storage = make_client(tmp_path)

    def boom(object_name):
        raise RuntimeError('missing object')

    fake_storage.delete_hook = boom
    response = client.delete('/objects/missing.txt')
    assert response.status_code == 500
    assert response.json()['detail'] == '删除对象失败：missing.txt。异常信息：missing object'


def test_batch_delete_objects_requires_at_least_one_name(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.post('/objects/batch-delete', json={'object_names': []})
    assert response.status_code == 400
    assert response.json()['detail'] == '至少要选择一个对象'


def test_batch_delete_objects_success(tmp_path):
    client, fake_storage = make_client(tmp_path)
    response = client.post(
        '/objects/batch-delete',
        json={'object_names': ['alpha.txt', 'folder/beta.txt', 'alpha.txt']},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['requested_count'] == 2
    assert payload['deleted_count'] == 2
    assert payload['failed_count'] == 0
    assert payload['deleted'] == ['alpha.txt', 'folder/beta.txt']
    assert fake_storage.deleted_objects == ['alpha.txt', 'folder/beta.txt']


def test_batch_delete_objects_trims_empty_names_and_preserves_first_seen_order(tmp_path):
    client, fake_storage = make_client(tmp_path)
    response = client.post(
        '/objects/batch-delete',
        json={'object_names': ['  ', 'gamma.txt', ' alpha.txt ', 'gamma.txt', '', 'alpha.txt']},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['requested_count'] == 2
    assert payload['deleted'] == ['gamma.txt', 'alpha.txt']
    assert fake_storage.deleted_objects == ['gamma.txt', 'alpha.txt']


def test_batch_delete_objects_partial_failure(tmp_path):
    client, fake_storage = make_client(tmp_path)

    def selective_delete(object_name):
        if object_name == 'folder/beta.txt':
            raise RuntimeError('locked')
        fake_storage.deleted_objects.append(object_name)

    fake_storage.delete_hook = selective_delete
    response = client.post(
        '/objects/batch-delete',
        json={'object_names': ['alpha.txt', 'folder/beta.txt', 'gamma.txt']},
    )
    assert response.status_code == 207
    payload = response.json()
    assert payload['ok'] is False
    assert payload['requested_count'] == 3
    assert payload['deleted_count'] == 2
    assert payload['failed_count'] == 1
    assert payload['deleted'] == ['alpha.txt', 'gamma.txt']
    assert payload['failed'] == [{'object_name': 'folder/beta.txt', 'detail': '异常信息：locked'}]
    assert payload['message'] == '批量删除部分完成：成功 2 个，失败 1 个。'
    assert payload['detail'] == '失败对象：folder/beta.txt'
    assert fake_storage.deleted_objects == ['alpha.txt', 'gamma.txt']


def test_batch_delete_objects_failure_when_all_fail(tmp_path):
    client, fake_storage = make_client(tmp_path)

    def always_fail(object_name):
        raise RuntimeError(f'boom-{object_name}')

    fake_storage.delete_hook = always_fail
    response = client.post(
        '/objects/batch-delete',
        json={'object_names': ['alpha.txt', 'beta.txt']},
    )
    assert response.status_code == 500
    payload = response.json()
    assert payload['ok'] is False
    assert payload['requested_count'] == 2
    assert payload['deleted_count'] == 0
    assert payload['failed_count'] == 2
    assert payload['failed'] == [
        {'object_name': 'alpha.txt', 'detail': '异常信息：boom-alpha.txt'},
        {'object_name': 'beta.txt', 'detail': '异常信息：boom-beta.txt'},
    ]
