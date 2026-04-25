from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings

from app.temp_uploads import TempUploadSessionStore
from app.upload_cleanup import UploadCleanupScheduler, UploadCleanupService, run_upload_cleanup
from app.upload_sessions import UploadSessionStore
from app.upload_tasks import ServerUploadTaskManager, ServerUploadTaskStore


class DummyThread:
    def __init__(self, alive: bool = True):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def iso_before(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def patch_json_file(path: Path, **updates) -> None:
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload.update(updates)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


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
        upload_session_dir=str(tmp_path / 'upload-sessions'),
        upload_task_dir=str(tmp_path / 'upload-tasks'),
        upload_temp_dir=str(tmp_path / 'upload-staging'),
        upload_proxy_chunk_size_mb=8,
        upload_cleanup_enabled=True,
        upload_cleanup_startup_enabled=True,
        upload_cleanup_scheduler_enabled=True,
        upload_cleanup_interval_seconds=3600,
        upload_completed_task_visible_seconds=1.0,
        upload_cleanup_completed_retention_hours=24,
        upload_cleanup_failed_retention_hours=72,
        upload_cleanup_stale_staging_retention_hours=24,
    )


def test_cleanup_removes_completed_task_files_and_metadata(tmp_path):
    settings = build_settings(tmp_path)
    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    temp_store = TempUploadSessionStore(settings.upload_temp_dir)
    session_store = UploadSessionStore(settings.upload_session_dir)

    staged_path = Path(settings.upload_temp_dir) / 'done.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'done')

    upload_session = session_store.create(
        object_name='done.bin',
        content_type='application/octet-stream',
        total_size=4,
        chunk_size=4,
        parallelism=1,
        strategy='oci-multipart-server-proxy',
        fingerprint='done-fingerprint',
        multipart_upload_id='mp-done',
    )
    temp_upload = temp_store.create(
        temp_upload_id='temp-done',
        filename='done.bin',
        object_name='done.bin',
        content_type='application/octet-stream',
        total_size=4,
        chunk_size=4,
        strategy='single-put-server-proxy',
        file_fingerprint='done-file',
        staged_path=str(staged_path),
    )
    patch_json_file(
        Path(settings.upload_temp_dir) / f'{temp_upload.temp_upload_id}.upload.json',
        committed=True,
        updated_at=iso_before(48),
        created_at=iso_before(48),
    )

    task = task_store.create(
        object_name='done.bin',
        filename='done.bin',
        content_type='application/octet-stream',
        total_size=4,
        strategy='oci-multipart-server-proxy',
        temp_path=str(staged_path),
        parallelism=1,
        total_parts=1,
        upload_session_id=upload_session.upload_id,
        multipart_upload_id='mp-done',
    )
    patch_json_file(
        Path(settings.upload_task_dir) / f'{task.task_id}.json',
        status='completed',
        phase='done',
        updated_at=iso_before(48),
        created_at=iso_before(48),
    )

    result = run_upload_cleanup(settings=settings, manager=None)

    assert task.task_id in result.deleted_task_files
    assert temp_upload.temp_upload_id in result.deleted_temp_upload_metadata
    assert upload_session.upload_id in result.deleted_upload_session_files
    assert str(staged_path.resolve()) in result.deleted_temp_files
    assert not staged_path.exists()
    assert task_store.get(task.task_id) is None
    assert temp_store.get(temp_upload.temp_upload_id) is None
    assert session_store.get(upload_session.upload_id) is None


def test_cleanup_removes_old_failed_task_temp_file(tmp_path):
    settings = build_settings(tmp_path)
    task_store = ServerUploadTaskStore(settings.upload_task_dir)

    staged_path = Path(settings.upload_temp_dir) / 'failed.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'failed')

    task = task_store.create(
        object_name='failed.bin',
        filename='failed.bin',
        content_type='application/octet-stream',
        total_size=6,
        strategy='single-put-server-proxy',
        temp_path=str(staged_path),
        parallelism=1,
        total_parts=None,
        upload_session_id=None,
        multipart_upload_id=None,
    )
    patch_json_file(
        Path(settings.upload_task_dir) / f'{task.task_id}.json',
        status='failed',
        phase='error',
        updated_at=iso_before(96),
        created_at=iso_before(96),
    )

    result = run_upload_cleanup(settings=settings, manager=None)
    assert task.task_id in result.deleted_task_files
    assert str(staged_path.resolve()) in result.deleted_temp_files
    assert task_store.get(task.task_id) is None


def test_cleanup_removes_stale_uncommitted_staging_session(tmp_path):
    settings = build_settings(tmp_path)
    temp_store = TempUploadSessionStore(settings.upload_temp_dir)

    staged_path = Path(settings.upload_temp_dir) / 'stale.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'stale')

    session = temp_store.create(
        temp_upload_id='temp-stale',
        filename='stale.bin',
        object_name='stale.bin',
        content_type='application/octet-stream',
        total_size=5,
        chunk_size=5,
        strategy='single-put-server-proxy',
        file_fingerprint='stale-fingerprint',
        staged_path=str(staged_path),
    )
    patch_json_file(
        Path(settings.upload_temp_dir) / f'{session.temp_upload_id}.upload.json',
        updated_at=iso_before(30),
        created_at=iso_before(30),
    )

    result = run_upload_cleanup(settings=settings, manager=None)
    assert session.temp_upload_id in result.deleted_temp_upload_metadata
    assert str(staged_path.resolve()) in result.deleted_temp_files
    assert temp_store.get(session.temp_upload_id) is None
    assert not staged_path.exists()


def test_cleanup_skips_active_task_and_related_files(tmp_path):
    settings = build_settings(tmp_path)
    task_store = ServerUploadTaskStore(settings.upload_task_dir)
    temp_store = TempUploadSessionStore(settings.upload_temp_dir)
    session_store = UploadSessionStore(settings.upload_session_dir)

    staged_path = Path(settings.upload_temp_dir) / 'active.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'active')

    upload_session = session_store.create(
        object_name='active.bin',
        content_type='application/octet-stream',
        total_size=6,
        chunk_size=6,
        parallelism=1,
        strategy='oci-multipart-server-proxy',
        fingerprint='active-fingerprint',
        multipart_upload_id='mp-active',
    )
    temp_upload = temp_store.create(
        temp_upload_id='temp-active',
        filename='active.bin',
        object_name='active.bin',
        content_type='application/octet-stream',
        total_size=6,
        chunk_size=6,
        strategy='single-put-server-proxy',
        file_fingerprint='active-file',
        staged_path=str(staged_path),
    )
    patch_json_file(
        Path(settings.upload_temp_dir) / f'{temp_upload.temp_upload_id}.upload.json',
        committed=True,
        updated_at=iso_before(48),
        created_at=iso_before(48),
    )

    task = task_store.create(
        object_name='active.bin',
        filename='active.bin',
        content_type='application/octet-stream',
        total_size=6,
        strategy='oci-multipart-server-proxy',
        temp_path=str(staged_path),
        parallelism=1,
        total_parts=1,
        upload_session_id=upload_session.upload_id,
        multipart_upload_id='mp-active',
    )
    patch_json_file(
        Path(settings.upload_task_dir) / f'{task.task_id}.json',
        status='running',
        phase='uploading_to_oci',
        updated_at=iso_before(48),
        created_at=iso_before(48),
    )

    manager = ServerUploadTaskManager(settings=settings, auto_recover=False)
    manager._threads[task.task_id] = DummyThread(alive=True)

    result = run_upload_cleanup(settings=settings, manager=manager)

    assert task.task_id in result.skipped_active_tasks
    assert temp_upload.temp_upload_id in result.skipped_active_temp_uploads
    assert upload_session.upload_id in result.skipped_active_upload_sessions
    assert task_store.get(task.task_id) is not None
    assert temp_store.get(temp_upload.temp_upload_id) is not None
    assert session_store.get(upload_session.upload_id) is not None
    assert staged_path.exists()


def test_cleanup_scheduler_runs_periodically(tmp_path):
    settings = build_settings(tmp_path)
    settings = Settings(**{**settings.__dict__, 'upload_cleanup_interval_seconds': 1})
    service = UploadCleanupService(settings=settings, manager=None)
    scheduler = UploadCleanupScheduler(service)

    staged_path = Path(settings.upload_temp_dir) / 'scheduled.bin'
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b'scheduled')
    session_path = Path(settings.upload_temp_dir) / 'scheduled.upload.json'
    session_path.write_text(
        json.dumps(
            {
                'temp_upload_id': 'scheduled',
                'filename': 'scheduled.bin',
                'object_name': 'scheduled.bin',
                'content_type': 'application/octet-stream',
                'total_size': 9,
                'chunk_size': 9,
                'strategy': 'single-put-server-proxy',
                'file_fingerprint': 'scheduled-fingerprint',
                'staged_path': str(staged_path),
                'created_at': iso_before(30),
                'updated_at': iso_before(30),
                'committed': False,
                'uploaded_chunks': {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    assert scheduler.start() is True
    deadline = time.time() + 3
    try:
        while time.time() < deadline:
            if not staged_path.exists() and not session_path.exists():
                break
            time.sleep(0.1)
    finally:
        scheduler.stop()

    assert not staged_path.exists()
    assert not session_path.exists()
    assert scheduler.is_running() is False


def test_cleanup_scheduler_can_be_disabled(tmp_path):
    settings = build_settings(tmp_path)
    settings = Settings(**{**settings.__dict__, 'upload_cleanup_scheduler_enabled': False})
    service = UploadCleanupService(settings=settings, manager=None)
    scheduler = UploadCleanupScheduler(service)

    assert scheduler.start() is False
    assert scheduler.is_running() is False


def test_manual_cleanup_route_returns_deleted_entries(tmp_path):
    from tests.test_upload_routes import make_client

    client, _fake_storage, _manager = make_client(tmp_path)
    temp_dir = tmp_path / 'upload-staging'
    temp_dir.mkdir(parents=True, exist_ok=True)
    staged_path = temp_dir / 'manual.bin'
    staged_path.write_bytes(b'manual')

    session_path = temp_dir / 'manual.upload.json'
    session_path.write_text(
        json.dumps(
            {
                'temp_upload_id': 'manual',
                'filename': 'manual.bin',
                'object_name': 'manual.bin',
                'content_type': 'application/octet-stream',
                'total_size': 6,
                'chunk_size': 6,
                'strategy': 'single-put-server-proxy',
                'file_fingerprint': 'manual-fingerprint',
                'staged_path': str(staged_path),
                'created_at': iso_before(30),
                'updated_at': iso_before(30),
                'committed': False,
                'uploaded_chunks': {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    response = client.post('/api/server-uploads/cleanup')
    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['deleted_temp_upload_metadata'] == ['manual']
    assert str(staged_path.resolve()) in payload['deleted_temp_files']
    assert not staged_path.exists()
    assert not session_path.exists()
