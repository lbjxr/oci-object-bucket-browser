import json

from app.config import Settings, get_settings
from app.security import verify_password
from app.settings_store import AppSettingsStore, reset_app_settings_store
from app.trash_store import reset_trash_record_store


def build_settings(tmp_path) -> Settings:
    return Settings(
        oci_config_path='~/.oci/config',
        oci_profile='DEFAULT',
        namespace='deployment-ns',
        bucket_name='deployment-bucket',
        compartment_id=None,
        settings_file=str(tmp_path / 'settings.json'),
    )


def update_payload(*, read_only=False, trash_enabled=True, batch_confirmation=True, password='webdav-password'):
    return {
        'access': {'read_only_mode': read_only},
        'storage': {
            'namespace': 'local-ns',
            'bucket_name': 'local-bucket',
            'region': 'ap-singapore-1',
            'prefix_root': 'team-assets',
        },
        'upload': {
            'chunk_size_mb': 24,
            'parallelism': 5,
            'single_put_threshold_mb': 48,
        },
        'safety': {
            'trash_enabled': trash_enabled,
            'batch_delete_confirmation_required': batch_confirmation,
        },
        'webdav': {
            'enabled': True,
            'username': 'dav-user',
            'password': password,
        },
    }


def test_settings_store_persists_without_plaintext_secret(tmp_path):
    settings = build_settings(tmp_path)
    store = AppSettingsStore(settings.settings_file, settings)

    public = store.update(update_payload())
    raw = json.loads((tmp_path / 'settings.json').read_text(encoding='utf-8'))

    assert public['access']['read_only_mode'] is False
    assert public['storage']['prefix_root'] == 'team-assets/'
    assert public['webdav']['password_configured'] is True
    assert 'password_hash' not in public['webdav']
    assert 'webdav-password' not in (tmp_path / 'settings.json').read_text(encoding='utf-8')
    assert verify_password('webdav-password', raw['webdav']['password_hash']) is True

    reloaded = AppSettingsStore(settings.settings_file, settings)
    assert reloaded.public_snapshot()['upload']['parallelism'] == 5
    assert reloaded.read_only_enabled() is False


def test_environment_values_override_local_storage(monkeypatch, tmp_path):
    settings = build_settings(tmp_path)
    store = AppSettingsStore(settings.settings_file, settings)
    store.update(update_payload())
    monkeypatch.setenv('OCI_BUCKET_NAME', 'environment-bucket')
    monkeypatch.setenv('APP_UPLOAD_PARALLELISM', '9')

    public = store.public_snapshot()
    effective = store.effective_settings()

    assert public['effective']['bucket_name'] == 'environment-bucket'
    assert public['environment_overrides']['OCI_BUCKET_NAME'] is True
    assert effective.bucket_name == 'environment-bucket'
    assert effective.upload_parallelism == 9


def test_settings_page_and_read_only_mode_block_object_writes(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_SETTINGS_FILE', str(tmp_path / 'app-settings.json'))
    get_settings.cache_clear()
    reset_app_settings_store()

    from tests.test_upload_routes import make_client

    client, _fake_storage, _manager = make_client(tmp_path)
    page = client.get('/settings')
    assert page.status_code == 200
    assert '/static/js/settings.js' in page.text

    saved = client.post('/api/settings', json=update_payload(read_only=True))
    assert saved.status_code == 200
    assert saved.json()['settings']['webdav']['password_configured'] is True
    assert 'password_hash' not in saved.text

    blocked = client.post('/api/files/folders', json={'prefix': '', 'folder_name': 'blocked'})
    assert blocked.status_code == 403
    assert '只读模式' in blocked.json()['detail']

    readable = client.get('/api/files')
    assert readable.status_code == 200

    reset_app_settings_store()
    reset_trash_record_store()
    get_settings.cache_clear()


def test_batch_delete_requires_exact_selected_count(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_SETTINGS_FILE', str(tmp_path / 'app-settings.json'))
    monkeypatch.setenv('APP_TRASH_RECORD_FILE', str(tmp_path / 'trash-records.json'))
    get_settings.cache_clear()
    reset_app_settings_store()
    reset_trash_record_store()

    from tests.test_upload_routes import make_client

    client, fake_storage, _manager = make_client(tmp_path)
    response = client.post('/objects/batch-delete', json={'object_names': ['a.txt', 'b.txt']})
    assert response.status_code == 400
    assert '输入所选对象数量 2' in response.json()['detail']
    assert fake_storage.deleted_objects == []

    wrong = client.post(
        '/objects/batch-delete',
        json={'object_names': ['a.txt', 'b.txt'], 'confirmation_count': 1},
    )
    assert wrong.status_code == 400
    assert fake_storage.deleted_objects == []

    reset_app_settings_store()
    reset_trash_record_store()
    get_settings.cache_clear()


def test_confirmation_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_SETTINGS_FILE', str(tmp_path / 'app-settings.json'))
    get_settings.cache_clear()
    reset_app_settings_store()

    from tests.test_upload_routes import make_client

    client, fake_storage, _manager = make_client(tmp_path)
    saved = client.post(
        '/api/settings',
        json=update_payload(trash_enabled=False, batch_confirmation=False),
    )
    assert saved.status_code == 200

    response = client.post('/objects/batch-delete', json={'object_names': ['a.txt', 'b.txt']})
    assert response.status_code == 200
    assert fake_storage.deleted_objects == ['a.txt', 'b.txt']

    reset_app_settings_store()
    get_settings.cache_clear()


def test_settings_validate_reports_user_error_for_invalid_values(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_SETTINGS_FILE', str(tmp_path / 'app-settings.json'))
    get_settings.cache_clear()
    reset_app_settings_store()

    from tests.test_upload_routes import make_client

    client, _fake_storage, _manager = make_client(tmp_path)
    invalid = update_payload()
    invalid['upload']['parallelism'] = 0
    response = client.post('/api/settings/validate', json=invalid)
    assert response.status_code == 422
    assert '并发数' in response.json()['detail']

    reset_app_settings_store()
    get_settings.cache_clear()


def test_trash_mode_copies_records_and_then_deletes(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_SETTINGS_FILE', str(tmp_path / 'app-settings.json'))
    monkeypatch.setenv('APP_TRASH_RECORD_FILE', str(tmp_path / 'trash-records.json'))
    get_settings.cache_clear()
    reset_app_settings_store()
    reset_trash_record_store()

    from tests.test_upload_routes import make_client

    client, fake_storage, _manager = make_client(tmp_path)
    saved = client.post('/api/settings', json=update_payload(trash_enabled=True))
    assert saved.status_code == 200
    fake_storage.download_payloads = {'docs/a.txt': b'alpha'}

    response = client.delete('/objects/docs/a.txt')
    assert response.status_code == 200
    payload = response.json()
    assert payload['recycled'] is True
    assert payload['trash_key'].startswith('.trash/')
    assert payload['trash_key'].endswith('/docs/a.txt')
    assert fake_storage.single_uploads == [
        (payload['trash_key'], b'alpha', 'application/octet-stream'),
    ]
    assert fake_storage.deleted_objects == ['docs/a.txt']

    records = json.loads((tmp_path / 'trash-records.json').read_text(encoding='utf-8'))['records']
    assert len(records) == 1
    assert records[0]['original_key'] == 'docs/a.txt'
    assert records[0]['trash_key'] == payload['trash_key']
    assert records[0]['size'] == 5
    assert records[0]['deleted_by'] == 'web-ui'
    assert records[0]['status'] == 'deleted'
    assert records[0]['deleted_at']

    reset_app_settings_store()
    reset_trash_record_store()
    get_settings.cache_clear()


def test_trash_mode_covers_directory_and_batch_delete(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_SETTINGS_FILE', str(tmp_path / 'app-settings.json'))
    monkeypatch.setenv('APP_TRASH_RECORD_FILE', str(tmp_path / 'trash-records.json'))
    get_settings.cache_clear()
    reset_app_settings_store()
    reset_trash_record_store()

    from tests.test_upload_routes import make_client

    client, fake_storage, _manager = make_client(tmp_path)
    client.post('/api/settings', json=update_payload(trash_enabled=True))
    fake_storage.object_entries = [
        type('Obj', (), {'name': name, 'size': 1, 'etag': name, 'time_created': '', 'content_type': 'text/plain'})()
        for name in ('docs/a.txt', 'docs/b.txt')
    ]
    fake_storage.download_payloads = {
        'docs/a.txt': b'a',
        'docs/b.txt': b'b',
        'outside.txt': b'c',
    }

    directory = client.post('/api/files/delete', json={'path': 'docs/'})
    assert directory.status_code == 200
    assert directory.json()['deleted_count'] == 2
    assert directory.json()['recycled_count'] == 2

    batch = client.post(
        '/objects/batch-delete',
        json={'object_names': ['outside.txt'], 'confirmation_count': 1},
    )
    assert batch.status_code == 200
    assert len(batch.json()['recycled']) == 1
    assert fake_storage.deleted_objects == ['docs/a.txt', 'docs/b.txt', 'outside.txt']

    records = json.loads((tmp_path / 'trash-records.json').read_text(encoding='utf-8'))['records']
    assert [record['original_key'] for record in records] == ['docs/a.txt', 'docs/b.txt', 'outside.txt']
    assert [record['deleted_by'] for record in records] == ['web-ui', 'web-ui', 'web-ui-batch']

    reset_app_settings_store()
    reset_trash_record_store()
    get_settings.cache_clear()
