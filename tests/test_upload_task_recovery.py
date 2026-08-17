from __future__ import annotations

import json
import time
from pathlib import Path
import pytest
from app.config import Settings
from app.upload_sessions import UploadedPart, UploadSessionStore
from app.temp_uploads import UploadQuotaExceeded
from app.upload_tasks import ServerUploadTaskManager, ServerUploadTaskStore, describe_task_state


class FakeStorageBackend:
    def __init__(self):
        self.multipart_counter = 0
        self.single_uploads: list[tuple[str, bytes, str | None]] = []
        self.multipart_created: list[tuple[str, str | None, str]] = []
        self.remote_parts: dict[tuple[str, int], str] = {}
        self.part_payloads: dict[tuple[str, int], bytes] = {}
        self.upload_part_calls: list[tuple[str, int, int]] = []
        self.commits: list[tuple[str, str, list[tuple[int, str]]]] = []
        self.aborts: list[tuple[str, str]] = []
        self.fail_parts_remaining: dict[int, int] = {}
        self.fail_single_put_remaining: int = 0


class FakeOCIStorageService:
    backend: FakeStorageBackend | None = None

    def __init__(self, settings=None):
        if self.backend is None:
            raise RuntimeError('fake backend not initialized')
        self.settings = settings
        self.backend = self.__class__.backend

    def upload_file(self, object_name, fileobj, content_type=None):
        if self.backend.fail_single_put_remaining > 0:
            self.backend.fail_single_put_remaining -= 1
            raise TimeoutError('single-put timeout')
        self.backend.single_uploads.append((object_name, fileobj.read(), content_type))

    def create_multipart_upload(self, object_name, content_type=None):
        self.backend.multipart_counter += 1
        upload_id = f'mp-{self.backend.multipart_counter}'
        self.backend.multipart_created.append((object_name, content_type, upload_id))
        return upload_id

    def upload_part(self, *, object_name, multipart_upload_id, part_num, payload, content_type=None):
        self.backend.upload_part_calls.append((multipart_upload_id, part_num, len(payload)))
        remaining = self.backend.fail_parts_remaining.get(part_num, 0)
        if remaining > 0:
          self.backend.fail_parts_remaining[part_num] = remaining - 1
          raise TimeoutError(f'part-{part_num}-timeout')
        self.backend.part_payloads[(multipart_upload_id, part_num)] = payload
        etag = f'etag-{part_num}'
        self.backend.remote_parts[(multipart_upload_id, part_num)] = etag
        return etag

    def list_multipart_uploaded_parts(self, *, object_name, multipart_upload_id):
        return {
            part_num: etag
            for (upload_id, part_num), etag in self.backend.remote_parts.items()
            if upload_id == multipart_upload_id
        }

    def commit_multipart_upload(self, *, object_name, multipart_upload_id, parts):
        self.backend.commits.append((object_name, multipart_upload_id, parts))

    def abort_multipart_upload(self, *, object_name, multipart_upload_id):
        self.backend.aborts.append((object_name, multipart_upload_id))


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        oci_config_path='~/.oci/config',
        oci_profile='DEFAULT',
        namespace='ns',
        bucket_name='bucket',
        compartment_id=None,
        upload_chunk_size_mb=8,
        upload_single_put_threshold_mb=4,
        upload_parallelism=3,
        upload_completed_task_visible_seconds=1.0,
        upload_session_dir=str(tmp_path / 'upload-sessions'),
        upload_task_dir=str(tmp_path / 'upload-tasks'),
        upload_temp_dir=str(tmp_path / 'upload-staging'),
        upload_proxy_chunk_size_mb=8,
    )


def wait_for_task_completion(manager: ServerUploadTaskManager, task_id: str, *, timeout_seconds: float = 5.0):
    import time

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        task = manager.task_store.get(task_id)
        if task and task.status in {'completed', 'failed', 'canceled'}:
            return task
        time.sleep(0.05)
    raise AssertionError(f'task {task_id} did not finish in time')


def wait_for_task_absence(manager: ServerUploadTaskManager, task_id: str, *, timeout_seconds: float = 5.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if manager.task_store.get(task_id) is None:
            return
        time.sleep(0.05)
    raise AssertionError(f'task {task_id} still exists after waiting')


def test_failed_task_can_be_requeued_and_completed_tasks_can_be_cleared(monkeypatch, tmp_path):
    settings = build_settings(tmp_path)
    staged_path = tmp_path / 'upload-staging' / 'retry.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'retry')
    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    failed = task_store.create(
        object_name='retry.bin',
        filename='retry.bin',
        content_type='application/octet-stream',
        total_size=5,
        strategy='single-put-server-proxy',
        temp_path=str(staged_path),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )
    task_store.update(failed.task_id, lambda task: (setattr(task, 'status', 'failed'), setattr(task, 'phase', 'error'), setattr(task, 'error', 'timeout')))
    completed = task_store.create(
        object_name='done.bin',
        filename='done.bin',
        content_type='application/octet-stream',
        total_size=5,
        strategy='single-put-server-proxy',
        temp_path=str(staged_path),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )
    task_store.update(completed.task_id, lambda task: setattr(task, 'status', 'completed'))

    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    started = []
    monkeypatch.setattr(manager, 'start', lambda task_id: started.append(task_id) or True)

    retried = manager.retry(failed.task_id)

    assert retried is not None
    assert retried.status == 'queued'
    assert retried.phase == 'manual_retry_queued'
    assert retried.error is None
    assert started == [failed.task_id]
    assert manager.clear_completed() == 1
    assert manager.task_store.get(completed.task_id) is None


def test_recover_running_multipart_task_completes(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    settings = build_settings(tmp_path)
    chunk_size = settings.upload_chunk_size_mb * 1024 * 1024
    total_size = 10 * 1024 * 1024
    staged_path = tmp_path / 'upload-staging' / 'recover-running.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'a' * (8 * 1024 * 1024) + b'b' * (2 * 1024 * 1024))

    session_store = UploadSessionStore(settings.upload_session_dir)
    session = session_store.create(
        object_name='recover-running.bin',
        content_type='application/octet-stream',
        total_size=total_size,
        chunk_size=chunk_size,
        parallelism=settings.upload_parallelism,
        strategy='oci-multipart-server-proxy',
        fingerprint='server-task:recover-running',
        multipart_upload_id='mp-existing',
    )

    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    task = task_store.create(
        object_name='recover-running.bin',
        filename='recover-running.bin',
        content_type='application/octet-stream',
        total_size=total_size,
        strategy='oci-multipart-server-proxy',
        temp_path=str(staged_path),
        parallelism=settings.upload_parallelism,
        total_parts=2,
        upload_session_id=session.upload_id,
        multipart_upload_id='mp-existing',
    )
    task_store.update(
        task.task_id,
        lambda t: (
            setattr(t, 'status', 'running'),
            setattr(t, 'phase', 'uploading_parts:1/2'),
            setattr(t, 'uploaded_parts', [1]),
            setattr(t, 'uploaded_bytes', 8 * 1024 * 1024),
        ),
    )
    backend.remote_parts[('mp-existing', 1)] = 'etag-1'
    session_store.update(
        session.upload_id,
        lambda s: s.uploaded_parts.__setitem__(1, UploadedPart(part_num=1, etag='etag-1', size=8 * 1024 * 1024)),
    )

    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    manager._completed_task_grace_period_seconds = 0.3
    recovered = manager.recover_incomplete_tasks()
    assert recovered == [task.task_id]

    finished = wait_for_task_completion(manager, task.task_id)
    assert finished.status == 'completed'
    assert finished.phase == 'done'
    assert finished.uploaded_parts == [1, 2]
    assert finished.uploaded_bytes == total_size
    assert backend.upload_part_calls == [('mp-existing', 2, 2 * 1024 * 1024)]
    assert backend.commits == [
        ('recover-running.bin', 'mp-existing', [(1, 'etag-1'), (2, 'etag-2')])
    ]
    assert not staged_path.exists()
    wait_for_task_absence(manager, task.task_id)


def test_recover_finalizing_task_commits_without_reupload(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    settings = build_settings(tmp_path)
    staged_path = tmp_path / 'upload-staging' / 'recover-finalizing.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'a' * (8 * 1024 * 1024) + b'b' * (2 * 1024 * 1024))

    session_store = UploadSessionStore(settings.upload_session_dir)
    session = session_store.create(
        object_name='recover-finalizing.bin',
        content_type='application/octet-stream',
        total_size=10 * 1024 * 1024,
        chunk_size=8 * 1024 * 1024,
        parallelism=settings.upload_parallelism,
        strategy='oci-multipart-server-proxy',
        fingerprint='server-task:recover-finalizing',
        multipart_upload_id='mp-final',
    )
    session_store.update(
        session.upload_id,
        lambda s: s.uploaded_parts.update(
            {
                1: UploadedPart(part_num=1, etag='etag-1', size=8 * 1024 * 1024),
                2: UploadedPart(part_num=2, etag='etag-2', size=2 * 1024 * 1024),
            }
        ),
    )
    backend.remote_parts[('mp-final', 1)] = 'etag-1'
    backend.remote_parts[('mp-final', 2)] = 'etag-2'

    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    task = task_store.create(
        object_name='recover-finalizing.bin',
        filename='recover-finalizing.bin',
        content_type='application/octet-stream',
        total_size=10 * 1024 * 1024,
        strategy='oci-multipart-server-proxy',
        temp_path=str(staged_path),
        parallelism=settings.upload_parallelism,
        total_parts=2,
        upload_session_id=session.upload_id,
        multipart_upload_id='mp-final',
    )
    task_store.update(
        task.task_id,
        lambda t: (
            setattr(t, 'status', 'finalizing'),
            setattr(t, 'phase', 'finalizing'),
            setattr(t, 'uploaded_parts', [1, 2]),
            setattr(t, 'uploaded_bytes', 10 * 1024 * 1024),
        ),
    )

    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    manager._completed_task_grace_period_seconds = 0.3
    recovered = manager.recover_incomplete_tasks()
    assert recovered == [task.task_id]

    finished = wait_for_task_completion(manager, task.task_id)
    assert finished.status == 'completed'
    assert finished.phase == 'done'
    assert backend.upload_part_calls == []
    assert backend.commits == [
        ('recover-finalizing.bin', 'mp-final', [(1, 'etag-1'), (2, 'etag-2')])
    ]
    wait_for_task_absence(manager, task.task_id)


def test_recover_queued_single_put_task_completes(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    settings = build_settings(tmp_path)
    staged_path = tmp_path / 'upload-staging' / 'recover-single.txt'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'hello recovery')

    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    task = task_store.create(
        object_name='recover-single.txt',
        filename='recover-single.txt',
        content_type='text/plain',
        total_size=len(b'hello recovery'),
        strategy='single-put-server-proxy',
        temp_path=str(staged_path),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )

    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    manager._completed_task_grace_period_seconds = 0.3
    recovered = manager.recover_incomplete_tasks()
    assert recovered == [task.task_id]

    finished = wait_for_task_completion(manager, task.task_id)
    assert finished.status == 'completed'
    assert finished.phase == 'done'
    assert backend.single_uploads == [('recover-single.txt', b'hello recovery', 'text/plain')]
    assert not staged_path.exists()
    wait_for_task_absence(manager, task.task_id)


def test_recover_missing_temp_file_marks_task_failed(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    settings = build_settings(tmp_path)
    missing_path = tmp_path / 'upload-staging' / 'missing.bin'

    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    task = task_store.create(
        object_name='missing.bin',
        filename='missing.bin',
        content_type='application/octet-stream',
        total_size=123,
        strategy='single-put-server-proxy',
        temp_path=str(missing_path),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )
    task_store.update(task.task_id, lambda t: setattr(t, 'status', 'running'))

    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    recovered = manager.recover_incomplete_tasks()
    assert recovered == []

    failed = manager.task_store.get(task.task_id)
    assert failed is not None
    assert failed.status == 'failed'
    assert failed.phase == 'recovery_missing_temp_file'
    assert failed.error == '服务重启后恢复失败：暂存文件不存在'
    assert backend.single_uploads == []


def test_multipart_task_retries_retryable_part_and_then_succeeds(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    backend.fail_parts_remaining[2] = 1
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    settings = build_settings(tmp_path)
    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    manager._base_retry_delay_seconds = 0.01
    manager._max_retry_delay_seconds = 0.02
    manager._completed_task_grace_period_seconds = 0.3

    staged_path = tmp_path / 'upload-staging' / 'retry-part.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'a' * (8 * 1024 * 1024) + b'b' * (2 * 1024 * 1024))

    task = manager.create_task_from_staged_file(
        filename='retry-part.bin',
        content_type='application/octet-stream',
        staged_path=str(staged_path),
        total_size=10 * 1024 * 1024,
    )
    finished = wait_for_task_completion(manager, task.task_id)
    assert finished.status == 'completed'
    assert finished.phase == 'done'
    part2_calls = [call for call in backend.upload_part_calls if call[1] == 2]
    assert len(part2_calls) == 2
    wait_for_task_absence(manager, task.task_id)



def test_single_put_task_retries_retryable_error_and_then_succeeds(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    backend.fail_single_put_remaining = 1
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    settings = build_settings(tmp_path)
    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    manager._base_retry_delay_seconds = 0.01
    manager._max_retry_delay_seconds = 0.02
    manager._completed_task_grace_period_seconds = 0.3

    staged_path = tmp_path / 'upload-staging' / 'retry-single.txt'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'retry single put')

    task = manager.create_task_from_staged_file(
        filename='retry-single.txt',
        content_type='text/plain',
        staged_path=str(staged_path),
        total_size=len(b'retry single put'),
    )
    finished = wait_for_task_completion(manager, task.task_id)
    assert finished.status == 'completed'
    assert finished.phase == 'done'
    assert backend.single_uploads == [('retry-single.txt', b'retry single put', 'text/plain')]
    wait_for_task_absence(manager, task.task_id)


def test_cancel_during_retry_wait_keeps_canceled_status(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    backend.fail_single_put_remaining = 99
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    settings = build_settings(tmp_path)
    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    manager._base_retry_delay_seconds = 0.2
    manager._max_retry_delay_seconds = 0.2
    manager._completed_task_grace_period_seconds = 0.3

    staged_path = tmp_path / 'upload-staging' / 'cancel-retry.txt'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'cancel retry')

    task = manager.create_task_from_staged_file(
        filename='cancel-retry.txt',
        content_type='text/plain',
        staged_path=str(staged_path),
        total_size=len(b'cancel retry'),
    )

    deadline = time.time() + 5.0
    while time.time() < deadline:
        current = manager.task_store.get(task.task_id)
        if current and current.phase.startswith('retrying_single_put:'):
            break
        time.sleep(0.02)
    else:
        raise AssertionError('task did not enter retrying state in time')

    canceled = manager.cancel(task.task_id)
    assert canceled is not None

    finished = wait_for_task_completion(manager, task.task_id)
    assert finished.status == 'canceled'
    assert finished.phase == 'canceled'
    assert finished.error is None



def test_completed_task_visible_window_uses_settings_value(monkeypatch, tmp_path):
    backend = FakeStorageBackend()
    FakeOCIStorageService.backend = backend
    monkeypatch.setattr('app.upload_tasks.OCIStorageService', FakeOCIStorageService)

    base_settings = build_settings(tmp_path)
    settings = Settings(**{**base_settings.__dict__, 'upload_completed_task_visible_seconds': 0.0})
    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)

    staged_path = tmp_path / 'upload-staging' / 'visible-window.txt'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'visible-window')

    task = manager.create_task_from_staged_file(
        filename='visible-window.txt',
        content_type='text/plain',
        staged_path=str(staged_path),
        total_size=len(b'visible-window'),
    )

    wait_for_task_absence(manager, task.task_id, timeout_seconds=2.0)
    assert manager._completed_task_grace_period_seconds == 0.0
    assert backend.single_uploads == [('visible-window.txt', b'visible-window', 'text/plain')]



def test_describe_task_state_for_recovery_labels(tmp_path):
    settings = build_settings(tmp_path)
    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    task = task_store.create(
        object_name='recover-label.bin',
        filename='recover-label.bin',
        content_type='application/octet-stream',
        total_size=123,
        strategy='single-put-server-proxy',
        temp_path=str(tmp_path / 'upload-staging' / 'recover-label.bin'),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )
    task_store.update(
        task.task_id,
        lambda t: (
            setattr(t, 'status', 'queued'),
            setattr(t, 'phase', 'recovery_requeued_from:finalizing'),
        ),
    )

    refreshed = task_store.get(task.task_id)
    assert refreshed is not None
    payload = describe_task_state(refreshed)
    assert payload['current_phase'] == 'recovery_requeued_from:finalizing'
    assert payload['phase_label'] == '服务重启后已恢复，原状态：完成提交中'
    assert payload['recovered'] is True
    assert payload['recovery_attempted'] is True
    assert payload['recovery_source_status'] == 'finalizing'
    assert payload['recovery_problem'] is None
    assert payload['status_label'] == '排队中'
    assert payload['is_retrying'] is False
    assert payload['retry_count'] == 0
    assert payload['retry_attempt'] == 0
    assert payload['retry_max_attempts'] == 0
    assert payload['retry_kind'] is None
    assert payload['retry_part_num'] is None
    assert payload['retry_label'] is None
    assert payload['last_error'] is None


def test_describe_task_state_for_retrying_single_put(tmp_path):
    settings = build_settings(tmp_path)
    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    task = task_store.create(
        object_name='retry-single.bin',
        filename='retry-single.bin',
        content_type='application/octet-stream',
        total_size=123,
        strategy='single-put-server-proxy',
        temp_path=str(tmp_path / 'upload-staging' / 'retry-single.bin'),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )
    task_store.update(
        task.task_id,
        lambda t: (
            setattr(t, 'status', 'running'),
            setattr(t, 'phase', 'retrying_single_put:2/3'),
            setattr(t, 'error', 'single-put 上传失败，2.0 秒后重试：timeout'),
        ),
    )

    refreshed = task_store.get(task.task_id)
    assert refreshed is not None
    payload = describe_task_state(refreshed)
    assert payload['is_retrying'] is True
    assert payload['retry_count'] == 2
    assert payload['retry_attempt'] == 2
    assert payload['retry_max_attempts'] == 3
    assert payload['retry_kind'] == 'single_put'
    assert payload['retry_part_num'] is None
    assert payload['retry_label'] == '后台重试中（第 2 次，共 3 次）'
    assert payload['last_error'] == '后台上传服务暂时不可用，请稍后重试。'


def test_describe_task_state_for_retrying_part(tmp_path):
    settings = build_settings(tmp_path)
    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    task = task_store.create(
        object_name='retry-part.bin',
        filename='retry-part.bin',
        content_type='application/octet-stream',
        total_size=123,
        strategy='oci-multipart-server-proxy',
        temp_path=str(tmp_path / 'upload-staging' / 'retry-part.bin'),
        parallelism=2,
        total_parts=4,
        upload_session_id='upload-1',
        multipart_upload_id='mp-1',
    )
    task_store.update(
        task.task_id,
        lambda t: (
            setattr(t, 'status', 'running'),
            setattr(t, 'phase', 'retrying_part:3（第1次重试）'),
            setattr(t, 'error', '分片 3 上传失败，1.0 秒后重试：http_429'),
        ),
    )

    refreshed = task_store.get(task.task_id)
    assert refreshed is not None
    payload = describe_task_state(refreshed)
    assert payload['is_retrying'] is True
    assert payload['retry_count'] == 1
    assert payload['retry_attempt'] == 1
    assert payload['retry_max_attempts'] == 3
    assert payload['retry_kind'] == 'part'
    assert payload['retry_part_num'] == 3
    assert payload['retry_label'] == '后台重试中（分片 3，第 1 次）'
    assert payload['last_error'] == '后台上传服务暂时不可用，请稍后重试。'

def test_task_store_enforces_active_quota_atomically(tmp_path):
    store = ServerUploadTaskStore(str(tmp_path / 'tasks'))
    kwargs = {
        'object_name': 'one.bin',
        'filename': 'one.bin',
        'content_type': 'application/octet-stream',
        'total_size': 1,
        'strategy': 'single-put-server-proxy',
        'temp_path': str(tmp_path / 'one.bin'),
        'parallelism': 1,
        'total_parts': None,
        'upload_session_id': None,
        'multipart_upload_id': None,
        'max_active_tasks': 1,
    }
    store.create(**kwargs)

    with pytest.raises(UploadQuotaExceeded):
        store.create(**{**kwargs, 'object_name': 'two.bin', 'filename': 'two.bin'})


def test_manager_rejects_task_before_external_upload_when_quota_full(tmp_path):
    settings = build_settings(tmp_path)
    active_store = ServerUploadTaskStore(settings.upload_task_dir)
    active_store.create(
        object_name='active.bin',
        filename='active.bin',
        content_type='application/octet-stream',
        total_size=1,
        strategy='single-put-server-proxy',
        temp_path=str(tmp_path / 'active.bin'),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )
    limited_settings = Settings(**{**settings.__dict__, 'upload_max_active_tasks': 1})
    manager = ServerUploadTaskManager(settings=limited_settings, auto_recover=False)

    with pytest.raises(UploadQuotaExceeded):
        manager.create_task_from_staged_file(
            filename='blocked.bin',
            content_type='application/octet-stream',
            staged_path=str(tmp_path / 'blocked.bin'),
            total_size=1,
        )
