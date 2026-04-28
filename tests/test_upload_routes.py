import json
from pathlib import Path

from fastapi.testclient import TestClient
from oci.exceptions import ServiceError


class DummyManagerTask:
    def __init__(self, task_id='task-1', object_name='big.bin', strategy='oci-multipart-server-proxy'):
        self.task_id = task_id
        self.object_name = object_name
        self.filename = object_name
        self.content_type = 'application/octet-stream'
        self.total_size = 20 * 1024 * 1024
        self.strategy = strategy
        self.status = 'queued'
        self.phase = 'waiting'
        self.created_at = '2026-04-24T12:00:00+00:00'
        self.updated_at = self.created_at
        self.temp_path = '/tmp/fake-upload.bin'
        self.upload_session_id = 'upload-session-1'
        self.multipart_upload_id = 'mp-1'
        self.uploaded_bytes = 0
        self.uploaded_parts = []
        self.total_parts = 3
        self.parallelism = 3
        self.error = None

    def to_dict(self):
        return self.__dict__.copy()

    def to_api_dict(self):
        payload = self.to_dict()
        retrying = self.phase.startswith('retrying_')
        retry_count = 0
        retry_attempt = 0
        retry_max_attempts = 0
        retry_kind = None
        retry_part_num = None
        retry_label = None
        if self.phase.startswith('retrying_single_put:'):
            retry_kind = 'single_put'
            progress = self.phase.split(':', 1)[1]
            retry_attempt, retry_max_attempts = [int(x) for x in progress.split('/', 1)]
            retry_count = retry_attempt
            retry_label = f'后台重试中（第 {retry_attempt} 次，共 {retry_max_attempts} 次）'
        elif self.phase.startswith('retrying_part:') and '（第' in self.phase and '次重试）' in self.phase:
            retry_kind = 'part'
            progress = self.phase.split(':', 1)[1]
            part_text, attempt_text = progress.split('（第', 1)
            retry_part_num = int(part_text.strip())
            retry_attempt = int(attempt_text.split('次重试）', 1)[0])
            retry_max_attempts = 3
            retry_count = retry_attempt
            retry_label = f'后台重试中（分片 {retry_part_num}，第 {retry_attempt} 次）'
        payload.update({
            'current_phase': self.phase,
            'phase_label': '等待执行' if self.phase == 'waiting' else self.phase,
            'recovered': self.phase.startswith('recovery_'),
            'recovery_attempted': self.phase.startswith('recovery_') or bool(self.error and '恢复' in self.error),
            'recovery_source_status': self.phase.split(':', 1)[1] if self.phase.startswith('recovery_requeued_from:') else None,
            'recovery_problem': 'missing_temp_file' if self.phase == 'recovery_missing_temp_file' else None,
            'status_label': {
                'queued': '排队中',
                'running': '执行中',
                'finalizing': '收尾中',
                'completed': '已完成',
                'failed': '失败',
                'canceled': '已取消',
            }.get(self.status, self.status),
            'is_retrying': retrying,
            'retry_count': retry_count,
            'retry_attempt': retry_attempt,
            'retry_max_attempts': retry_max_attempts,
            'retry_kind': retry_kind,
            'retry_part_num': retry_part_num,
            'retry_label': retry_label,
            'last_error': self.error,
        })
        return payload


class DummyTaskStore:
    def __init__(self, task):
        self.task = task

    def list_recent(self, limit=20):
        return [self.task][:limit]

    def get(self, task_id):
        return self.task if self.task and self.task.task_id == task_id else None


class DummyUploadTaskManager:
    def __init__(self):
        self.created = []
        self.temp_root = Path('/tmp/server-upload-tests')
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.task = DummyManagerTask()
        self.task_store = DummyTaskStore(self.task)

    def temp_path_for(self, task_hint, filename):
        return self.temp_root / f'{task_hint}-{filename}'

    def create_task_from_staged_file(self, *, filename, content_type, staged_path, total_size):
        self.created.append((filename, content_type, staged_path, total_size))
        self.task.filename = filename
        self.task.object_name = filename
        self.task.total_size = total_size
        self.task.temp_path = staged_path
        return self.task

    def cancel(self, task_id):
        if self.task and self.task.task_id == task_id:
            self.task.status = 'canceled'
            return self.task
        return None


def make_client(tmp_path: Path):
    import app.routes as routes
    from app.config import get_settings
    from app.main import create_app

    import os
    os.environ['APP_AUTH_USERNAME'] = 'test-admin'
    os.environ['APP_AUTH_PASSWORD'] = 'test-password-for-smoke'
    os.environ['APP_SESSION_SECRET'] = 'test-session-secret-for-smoke'
    os.environ['APP_UPLOAD_SESSION_DIR'] = str(tmp_path / 'upload-sessions')
    os.environ['APP_UPLOAD_TEMP_DIR'] = str(tmp_path / 'upload-staging')
    os.environ['APP_UPLOAD_CHUNK_SIZE_MB'] = '8'
    os.environ['APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB'] = '4'
    os.environ['APP_UPLOAD_PARALLELISM'] = '3'
    get_settings.cache_clear()

    class FakeStorage:
        def __init__(self):
            self.single_uploads = []
            self.multipart_created = []
            self.parts = {}
            self.remote_parts = {}
            self.commits = []
            self.aborts = []
            self.upload_part_calls = []
            self.fail_parts = {}
            self.fail_part_errors = {}
            self.deleted_objects = []
            self.object_entries = []

        def upload_file(self, object_name, fileobj, content_type=None):
            payload = fileobj.read()
            self.single_uploads.append((object_name, payload, content_type))
            self.object_entries = [entry for entry in self.object_entries if entry.name != object_name]
            self.object_entries.append(type('Obj', (), {
                'name': object_name,
                'size': len(payload),
                'etag': f'etag-{object_name}',
                'time_created': '2026-04-24T12:00:00+00:00',
                'content_type': content_type or 'application/octet-stream',
            })())

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
            etag = f'etag-{part_num}'
            self.remote_parts[(multipart_upload_id, part_num)] = etag
            return etag

        def list_multipart_uploaded_parts(self, *, object_name, multipart_upload_id):
            return {
                part_num: etag
                for (upload_id, part_num), etag in self.remote_parts.items()
                if upload_id == multipart_upload_id
            }

        def commit_multipart_upload(self, *, object_name, multipart_upload_id, parts):
            self.commits.append((object_name, multipart_upload_id, parts))

        def abort_multipart_upload(self, *, object_name, multipart_upload_id):
            self.aborts.append((object_name, multipart_upload_id))

        def delete_object(self, object_name):
            if callable(getattr(self, 'delete_hook', None)):
                return self.delete_hook(object_name)
            self.deleted_objects.append(object_name)
            self.object_entries = [entry for entry in self.object_entries if entry.name != object_name]

        def list_objects(self, prefix=''):
            if not prefix:
                return list(self.object_entries)
            return [entry for entry in self.object_entries if entry.name.startswith(prefix)]

        def head_object(self, object_name):
            from app.models import ObjectDownloadInfo
            payload = getattr(self, 'download_payloads', {}).get(object_name)
            if payload is None:
                raise AssertionError(f'unexpected head_object for {object_name}')
            return ObjectDownloadInfo(
                size=len(payload),
                etag=f'etag-{object_name}',
                content_type='application/octet-stream',
            )

        def open_stream(self, object_name, *, range_header=None):
            payload = getattr(self, 'download_payloads', {}).get(object_name)
            if payload is None:
                raise AssertionError(f'unexpected open_stream for {object_name}')
            from io import BytesIO
            headers = {'content-length': str(len(payload)), 'etag': f'etag-{object_name}'}
            if range_header:
                unit, raw = range_header.split('=', 1)
                assert unit == 'bytes'
                start_text, end_text = raw.split('-', 1)
                start = int(start_text)
                end = int(end_text)
                sliced = payload[start:end + 1]
                headers['content-length'] = str(len(sliced))
                headers['content-range'] = f'bytes {start}-{end}/{len(payload)}'
                return BytesIO(sliced), 'application/octet-stream', headers
            return BytesIO(payload), 'application/octet-stream', headers

        def get_preview(self, object_name):
            raise AssertionError('not used in this test')

    fake_storage = FakeStorage()
    dummy_manager = DummyUploadTaskManager()
    routes.get_storage = lambda: fake_storage
    routes.get_upload_task_manager = lambda: dummy_manager
    client = TestClient(create_app())
    client.post('/login', data={'username': 'test-admin', 'password': 'test-password-for-smoke', 'next_path': '/'})
    return client, fake_storage, dummy_manager


def test_app_lifespan_starts_and_stops_cleanup_scheduler(tmp_path):
    import app.routes as routes
    from app.config import get_settings
    from app.main import create_app

    import os
    os.environ['APP_AUTH_USERNAME'] = 'test-admin'
    os.environ['APP_AUTH_PASSWORD'] = 'test-password-for-smoke'
    os.environ['APP_SESSION_SECRET'] = 'test-session-secret-for-smoke'
    os.environ['APP_UPLOAD_SESSION_DIR'] = str(tmp_path / 'upload-sessions')
    os.environ['APP_UPLOAD_TASK_DIR'] = str(tmp_path / 'upload-tasks')
    os.environ['APP_UPLOAD_TEMP_DIR'] = str(tmp_path / 'upload-staging')
    os.environ['APP_UPLOAD_CLEANUP_ENABLED'] = 'true'
    os.environ['APP_UPLOAD_CLEANUP_SCHEDULER_ENABLED'] = 'true'
    os.environ['APP_UPLOAD_CLEANUP_INTERVAL_SECONDS'] = '60'
    get_settings.cache_clear()

    routes.get_storage = lambda: type('FakeStorage', (), {'list_objects': lambda self, prefix='': []})()
    routes.get_upload_task_manager = lambda: DummyUploadTaskManager()

    with TestClient(create_app()) as client:
        scheduler = client.app.state.upload_cleanup_scheduler
        assert scheduler.is_running() is True

    assert scheduler.is_running() is False


def test_small_file_upload_still_uses_single_put(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
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
    client, _, _ = make_client(tmp_path)
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

def test_index_includes_server_proxy_upload_copy(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)

    class _Object:
        def __init__(self):
            self.name = 'docs/a.txt'
            self.size = 12
            self.etag = 'etag-a'
            self.time_created = '2026-04-22T10:00:00+00:00'
            self.content_type = 'text/plain'

    fake_storage.list_objects = lambda prefix='': [_Object()]

    response = client.get('/')
    assert response.status_code == 200
    html = response.text
    assert '浏览器先把文件传到本服务；再由服务端异步上传到 OCI' in html
    assert 'upload-task-list' in html
    assert '/api/server-uploads/init' in html
    assert '/api/server-uploads/tasks' in html
    assert '正在上传到服务器…' in html
    assert '浏览器上传阶段已完成' in html



def test_index_includes_dynamic_throttle_concurrency_copy(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)

    class _Object:
        def __init__(self):
            self.name = 'docs/a.txt'
            self.size = 12
            self.etag = 'etag-a'
            self.time_created = '2026-04-22T10:00:00+00:00'
            self.content_type = 'text/plain'

    fake_storage.list_objects = lambda prefix='': [_Object()]

    response = client.get('/')
    assert response.status_code == 200
    html = response.text
    assert '浏览器先把文件传到本服务；再由服务端异步上传到 OCI' in html
    assert 'upload-task-list' in html
    assert '/api/server-uploads/init' in html
    assert '/api/server-uploads/tasks' in html
    assert '正在上传到服务器…' in html
    assert '浏览器上传阶段已完成' in html
    assert '下载所选' in html
    assert '/objects/batch-download' in html



def test_index_shows_file_manager_panel_and_folder_actions(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)

    fake_storage.object_entries = [
        type('Obj', (), {
            'name': 'docs/',
            'size': 0,
            'etag': 'etag-docs',
            'time_created': '2026-04-22T10:00:00+00:00',
            'content_type': 'application/x-directory',
        })(),
        type('Obj', (), {
            'name': 'docs/a.txt',
            'size': 12,
            'etag': 'etag-a',
            'time_created': '2026-04-22T10:00:00+00:00',
            'content_type': 'text/plain',
        })(),
    ]

    response = client.get('/')
    assert response.status_code == 200
    html = response.text
    assert '文件管理' in html
    assert '新建文件夹' in html
    assert '返回上级' not in html
    assert 'Bucket 根目录' in html
    assert '当前目录' in html
    assert '上传 / 新建文件夹 / 重命名默认作用在这里。' in html
    assert '📁 docs/' in html
    assert '重命名' in html
    assert '删除目录' in html



def test_index_shows_breadcrumbs_for_nested_prefix(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {
            'name': 'docs/2026/report.txt',
            'size': 12,
            'etag': 'etag-report',
            'time_created': '2026-04-22T10:00:00+00:00',
            'content_type': 'text/plain',
        })(),
    ]

    response = client.get('/?prefix=docs/2026/')
    assert response.status_code == 200
    html = response.text
    assert '返回上级' in html
    assert 'Bucket 根目录' in html
    assert 'docs' in html
    assert '2026' in html
    assert '<code>docs/2026/</code>' in html



def test_list_files_api_returns_folder_and_file_split(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/', 'size': 0, 'etag': 'etag-docs', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'application/x-directory'})(),
        type('Obj', (), {'name': 'docs/a.txt', 'size': 12, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
        type('Obj', (), {'name': 'docs/sub/b.txt', 'size': 18, 'etag': 'etag-b', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]

    response = client.get('/api/files?prefix=docs/')
    assert response.status_code == 200
    payload = response.json()
    assert payload['prefix'] == 'docs/'
    assert payload['current_directory_label'] == 'docs/'
    assert payload['parent_prefix'] == ''
    assert [item['name'] for item in payload['breadcrumbs']] == ['Bucket 根目录', 'docs']
    assert [item['name'] for item in payload['folders']] == ['sub']
    assert [item['name'] for item in payload['files']] == ['docs/a.txt']



def test_create_folder_creates_placeholder_object(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    response = client.post('/api/files/folders', json={'prefix': 'docs/', 'folder_name': 'new-folder'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'docs/new-folder/'
    assert fake_storage.single_uploads[-1][0] == 'docs/new-folder/'
    assert fake_storage.single_uploads[-1][1] == b''



def test_create_folder_rejects_conflicting_prefix_without_overwrite(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/new-folder/', 'size': 0, 'etag': 'etag-docs', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'application/x-directory'})(),
        type('Obj', (), {'name': 'docs/new-folder/a.txt', 'size': 12, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]
    response = client.post('/api/files/folders', json={'prefix': 'docs/', 'folder_name': 'new-folder'})
    assert response.status_code == 409
    payload = response.json()
    assert payload['conflict']['action'] == 'create_folder'
    assert payload['conflict']['destination_path'] == 'docs/new-folder/'



def test_create_folder_allows_overwrite_after_confirmation(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/new-folder/', 'size': 0, 'etag': 'etag-docs', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'application/x-directory'})(),
    ]
    response = client.post('/api/files/folders', json={'prefix': 'docs/', 'folder_name': 'new-folder', 'overwrite': True})
    assert response.status_code == 200
    assert response.json()['overwritten'] is True
    assert fake_storage.single_uploads[-1][0] == 'docs/new-folder/'



def test_rename_file_copies_then_deletes(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {'docs/a.txt': b'hello-a'}
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/a.txt', 'size': 7, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]

    response = client.post('/api/files/rename', json={'source_path': 'docs/a.txt', 'new_name': 'renamed.txt'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['kind'] == 'file'
    assert payload['destination_path'] == 'docs/renamed.txt'
    assert fake_storage.single_uploads[-1][0] == 'docs/renamed.txt'
    assert fake_storage.deleted_objects[-1] == 'docs/a.txt'



def test_rename_file_rejects_existing_target_without_overwrite(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/a.txt', 'size': 7, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
        type('Obj', (), {'name': 'docs/renamed.txt', 'size': 8, 'etag': 'etag-b', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]
    response = client.post('/api/files/rename', json={'source_path': 'docs/a.txt', 'new_name': 'renamed.txt'})
    assert response.status_code == 409
    payload = response.json()
    assert payload['conflict']['kind'] == 'file'
    assert payload['conflict']['destination_path'] == 'docs/renamed.txt'



def test_rename_file_allows_overwrite_after_confirmation(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {'docs/a.txt': b'hello-a'}
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/a.txt', 'size': 7, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
        type('Obj', (), {'name': 'docs/renamed.txt', 'size': 8, 'etag': 'etag-b', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]
    response = client.post('/api/files/rename', json={'source_path': 'docs/a.txt', 'new_name': 'renamed.txt', 'overwrite': True})
    assert response.status_code == 200
    assert response.json()['overwritten'] is True
    assert fake_storage.single_uploads[-1][0] == 'docs/renamed.txt'



def test_rename_folder_moves_all_children(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/': b'',
        'docs/a.txt': b'hello-a',
        'docs/sub/b.txt': b'hello-b',
    }
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/', 'size': 0, 'etag': 'etag-docs', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'application/x-directory'})(),
        type('Obj', (), {'name': 'docs/a.txt', 'size': 7, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
        type('Obj', (), {'name': 'docs/sub/b.txt', 'size': 7, 'etag': 'etag-b', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]

    response = client.post('/api/files/rename', json={'source_path': 'docs/', 'new_name': 'archive'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['kind'] == 'folder'
    assert payload['destination_path'] == 'archive/'
    uploaded_names = [item[0] for item in fake_storage.single_uploads]
    assert 'archive/' in uploaded_names
    assert 'archive/a.txt' in uploaded_names
    assert 'archive/sub/b.txt' in uploaded_names
    assert set(fake_storage.deleted_objects) >= {'docs/', 'docs/a.txt', 'docs/sub/b.txt'}



def test_rename_folder_rejects_existing_target_prefix_without_overwrite(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/', 'size': 0, 'etag': 'etag-docs', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'application/x-directory'})(),
        type('Obj', (), {'name': 'docs/a.txt', 'size': 7, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
        type('Obj', (), {'name': 'archive/existing.txt', 'size': 7, 'etag': 'etag-b', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]
    response = client.post('/api/files/rename', json={'source_path': 'docs/', 'new_name': 'archive'})
    assert response.status_code == 409
    payload = response.json()
    assert payload['conflict']['kind'] == 'folder'
    assert payload['conflict']['destination_path'] == 'archive/'



def test_delete_folder_deletes_all_objects_under_prefix(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'docs/', 'size': 0, 'etag': 'etag-docs', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'application/x-directory'})(),
        type('Obj', (), {'name': 'docs/a.txt', 'size': 7, 'etag': 'etag-a', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
        type('Obj', (), {'name': 'docs/sub/b.txt', 'size': 7, 'etag': 'etag-b', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'text/plain'})(),
    ]

    response = client.post('/api/files/delete', json={'path': 'docs/'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['kind'] == 'folder'
    assert payload['deleted_count'] == 3
    assert set(fake_storage.deleted_objects) >= {'docs/', 'docs/a.txt', 'docs/sub/b.txt'}



def test_batch_download_returns_zip_of_selected_objects(tmp_path):
    import io
    import zipfile

    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/a.txt': b'hello-a',
        'images/b.png': b'hello-b',
    }

    response = client.post(
        '/objects/batch-download?prefix=docs/',
        json={'object_names': ['docs/a.txt', 'images/b.png', 'docs/a.txt']},
    )
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/zip')
    assert 'attachment; filename="oci-batch-docs-2items-' in response.headers['content-disposition']
    assert response.headers['x-batch-requested-count'] == '2'
    assert response.headers['x-batch-archived-count'] == '2'
    assert response.headers['x-batch-failed-count'] == '0'
    assert response.headers['x-batch-partial'] == '0'

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ['docs/a.txt', 'images/b.png']
    assert archive.read('docs/a.txt') == b'hello-a'
    assert archive.read('images/b.png') == b'hello-b'



def test_batch_download_accepts_form_post_for_native_browser_download(tmp_path):
    import io
    import zipfile

    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/a.txt': b'hello-a',
        'images/b.png': b'hello-b',
    }

    response = client.post(
        '/objects/batch-download',
        data={
            'prefix': 'docs/',
            'object_names': ['docs/a.txt', 'images/b.png', 'docs/a.txt'],
        },
    )
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/zip')
    assert 'attachment; filename="oci-batch-docs-2items-' in response.headers['content-disposition']
    assert response.headers['x-batch-requested-count'] == '2'
    assert response.headers['x-batch-archived-count'] == '2'
    assert response.headers['x-batch-failed-count'] == '0'
    assert response.headers['x-batch-partial'] == '0'

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ['docs/a.txt', 'images/b.png']



def test_batch_download_skips_failed_objects_and_emits_failure_manifest(tmp_path):
    import io
    import json as _json
    import zipfile

    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/a.txt': b'hello-a',
        'images/b.png': b'hello-b',
    }

    response = client.post(
        '/objects/batch-download?prefix=mixed/',
        json={'object_names': ['docs/a.txt', 'missing/c.txt', 'images/b.png']},
    )
    assert response.status_code == 200
    assert response.headers['x-batch-requested-count'] == '3'
    assert response.headers['x-batch-archived-count'] == '2'
    assert response.headers['x-batch-failed-count'] == '1'
    assert response.headers['x-batch-partial'] == '1'

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == [
        '_batch_download_failures.json',
        '_batch_download_failures.txt',
        'docs/a.txt',
        'images/b.png',
    ]
    manifest = _json.loads(archive.read('_batch_download_failures.json').decode('utf-8'))
    assert manifest['requested_count'] == 3
    assert manifest['archived_count'] == 2
    assert manifest['failed_count'] == 1
    assert manifest['failed'] == [
        {'object_name': 'missing/c.txt', 'detail': '异常信息：unexpected open_stream for missing/c.txt'}
    ]
    failure_text = archive.read('_batch_download_failures.txt').decode('utf-8')
    assert 'missing/c.txt' in failure_text
    assert '其他成功对象已正常导出' in failure_text



def test_batch_download_fails_when_all_objects_fail(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {}

    response = client.post(
        '/objects/batch-download',
        json={'object_names': ['missing/a.txt', 'missing/b.txt']},
    )
    assert response.status_code == 500
    assert response.json()['detail'] == '批量下载失败：所有对象都未能成功读取，未生成可用 ZIP。'



def test_download_supports_range_requests(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/a.txt': b'0123456789',
    }

    response = client.get('/download/docs/a.txt', headers={'Range': 'bytes=2-5'})
    assert response.status_code == 206
    assert response.content == b'2345'
    assert response.headers['accept-ranges'] == 'bytes'
    assert response.headers['content-range'] == 'bytes 2-5/10'
    assert response.headers['content-length'] == '4'
    assert response.headers['etag'] == 'etag-docs/a.txt'



def test_download_supports_suffix_range_requests(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/a.txt': b'0123456789',
    }

    response = client.get('/download/docs/a.txt', headers={'Range': 'bytes=-3'})
    assert response.status_code == 206
    assert response.content == b'789'
    assert response.headers['content-range'] == 'bytes 7-9/10'
    assert response.headers['content-length'] == '3'



def test_download_rejects_multi_range_requests(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    fake_storage.download_payloads = {
        'docs/a.txt': b'0123456789',
    }

    response = client.get('/download/docs/a.txt', headers={'Range': 'bytes=0-1,4-5'})
    assert response.status_code == 416
    assert response.json()['detail'] == '当前仅支持单段 Range 请求'



def test_multipart_flow_supports_resume_and_complete(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
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
    client, fake_storage, _ = make_client(tmp_path)
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



def test_batch_download_requires_selection(tmp_path):
    client, _, _ = make_client(tmp_path)
    response = client.post('/objects/batch-download', json={'object_names': []})
    assert response.status_code == 400
    assert response.json()['detail'] == '至少要选择一个对象'



def test_batch_download_form_requires_selection(tmp_path):
    client, _, _ = make_client(tmp_path)
    response = client.post('/objects/batch-download', data={'prefix': 'docs/'})
    assert response.status_code == 400
    assert response.json()['detail'] == '至少要选择一个对象'



def test_resume_reconciles_remote_parts_when_local_session_is_stale(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'remote-reconcile',
        },
    )
    upload_id = init.json()['upload_id']
    session_file = tmp_path / 'upload-sessions' / f'{upload_id}.json'

    client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    fake_storage.remote_parts[('mp-1', 2)] = 'etag-2'

    payload = json.loads(session_file.read_text(encoding='utf-8'))
    payload['uploaded_parts'] = {}
    session_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    resumed = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'remote-reconcile',
        },
    )
    assert resumed.status_code == 200
    resumed_payload = resumed.json()
    assert resumed_payload['reused'] is True
    assert resumed_payload['reconciled_with_remote'] is True
    assert resumed_payload['remote_reconcile_degraded'] is False
    assert resumed_payload['remote_reconcile_warning'] is None
    assert resumed_payload['uploaded_parts'] == [1, 2]
    assert resumed_payload['uploaded_bytes'] == 16 * 1024 * 1024



def test_resume_degrades_to_local_session_when_remote_reconcile_fails(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'reconcile-fallback',
        },
    )
    upload_id = init.json()['upload_id']

    client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )

    def boom(**kwargs):
        raise RuntimeError('remote list failed')

    fake_storage.list_multipart_uploaded_parts = boom

    resumed = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'reconcile-fallback',
        },
    )
    assert resumed.status_code == 200
    resumed_payload = resumed.json()
    assert resumed_payload['reused'] is True
    assert resumed_payload['reconciled_with_remote'] is False
    assert resumed_payload['remote_reconcile_degraded'] is True
    assert '本次未完成 OCI 远端分片对账' in resumed_payload['remote_reconcile_warning']
    assert resumed_payload['uploaded_parts'] == [1]
    assert resumed_payload['uploaded_bytes'] == 8 * 1024 * 1024



def test_status_reconcile_removes_local_parts_missing_on_remote(tmp_path):
    client, _, _ = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'drop-ghost-part',
        },
    )
    upload_id = init.json()['upload_id']

    client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )

    session_file = tmp_path / 'upload-sessions' / f'{upload_id}.json'
    payload = json.loads(session_file.read_text(encoding='utf-8'))
    payload['uploaded_parts']['2'] = {
        'part_num': 2,
        'etag': 'ghost-etag-2',
        'size': 8 * 1024 * 1024,
    }
    session_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    status = client.get(f'/api/uploads/{upload_id}')
    assert status.status_code == 200
    payload = status.json()
    assert payload['reconciled_with_remote'] is True
    assert payload['remote_reconcile_degraded'] is False
    assert payload['remote_reconcile_warning'] is None
    assert payload['uploaded_parts'] == [1]
    assert payload['uploaded_bytes'] == 8 * 1024 * 1024



def test_complete_reconciles_remote_parts_before_commit(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'complete-reconcile',
        },
    )
    upload_id = init.json()['upload_id']

    client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    fake_storage.remote_parts[('mp-1', 2)] = 'etag-2'
    fake_storage.remote_parts[('mp-1', 3)] = 'etag-3'

    complete = client.post(f'/api/uploads/{upload_id}/complete')
    assert complete.status_code == 200
    assert fake_storage.commits[0][2] == [(1, 'etag-1'), (2, 'etag-2'), (3, 'etag-3')]



def test_cancel_upload_aborts_multipart(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
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
    client, _, _ = make_client(tmp_path)
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
    client, fake_storage, _ = make_client(tmp_path)
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
    client, fake_storage, _ = make_client(tmp_path)
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
    client, fake_storage, _ = make_client(tmp_path)
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
    client, fake_storage, _ = make_client(tmp_path)
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


def test_upload_part_http_429_is_retryable_and_returns_retry_after(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    init = client.post(
        '/api/uploads/init',
        json={
            'filename': 'big.bin',
            'file_size': 20 * 1024 * 1024,
            'content_type': 'application/octet-stream',
            'file_fingerprint': 'http-429-part',
        },
    )
    upload_id = init.json()['upload_id']
    fake_storage.fail_part_errors[1] = lambda: ServiceError(
        status=429,
        code='TooManyRequests',
        headers={'retry-after': '7'},
        message='slow down',
        request_endpoint='/uploadPart',
        client_version='test',
        timestamp='now',
        opc_request_id='req-429',
    )

    response = client.put(
        f'/api/uploads/{upload_id}/part/1',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert response.status_code == 429
    payload = response.json()
    assert payload['retryable'] is True
    assert payload['error_code'] == 'http_429'
    assert payload['retry_after_seconds'] == 7
    assert '建议等待约 7 秒后再试' in payload['reason']


def test_delete_object_requires_login(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    client.cookies.clear()
    response = client.delete('/objects/sample.txt')
    assert response.status_code == 401
    assert response.json()['detail'] == '未登录'
    assert fake_storage.deleted_objects == []


def test_delete_object_success(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
    response = client.delete('/objects/folder%2Fsample.txt')
    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['object_name'] == 'folder/sample.txt'
    assert payload['message'] == '已删除对象：folder/sample.txt'
    assert payload['detail'] == '对象“folder/sample.txt”已从 bucket 中移除。'
    assert fake_storage.deleted_objects == ['folder/sample.txt']


def test_delete_object_failure_is_reported(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)

    def boom(object_name):
        raise RuntimeError('missing object')

    fake_storage.delete_hook = boom
    response = client.delete('/objects/missing.txt')
    assert response.status_code == 500
    assert response.json()['detail'] == '删除对象失败：missing.txt。异常信息：missing object'


def test_batch_delete_objects_requires_at_least_one_name(tmp_path):
    client, _, _ = make_client(tmp_path)
    response = client.post('/objects/batch-delete', json={'object_names': []})
    assert response.status_code == 400
    assert response.json()['detail'] == '至少要选择一个对象'


def test_batch_delete_objects_success(tmp_path):
    client, fake_storage, _ = make_client(tmp_path)
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
    client, fake_storage, _ = make_client(tmp_path)
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
    client, fake_storage, _ = make_client(tmp_path)

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
    client, fake_storage, _ = make_client(tmp_path)

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
