from datetime import datetime, timedelta, timezone


def stats_client(tmp_path):
    from tests.test_upload_routes import make_client

    client, fake_storage, manager = make_client(tmp_path)
    now = datetime.now(timezone.utc)
    fake_storage.object_entries = [
        type('Obj', (), {
            'name': 'docs/', 'size': 0, 'etag': 'folder',
            'time_created': now.isoformat(), 'content_type': 'application/x-directory',
        })(),
        type('Obj', (), {
            'name': 'docs/report.pdf', 'size': 300, 'etag': 'pdf',
            'time_created': (now - timedelta(days=1)).isoformat(), 'content_type': 'application/pdf',
        })(),
        type('Obj', (), {
            'name': 'image.png', 'size': 100, 'etag': 'image',
            'time_created': (now - timedelta(days=2)).isoformat(), 'content_type': 'image/png',
        })(),
        type('Obj', (), {
            'name': 'archive.zip', 'size': 600, 'etag': 'archive',
            'time_created': (now - timedelta(days=20)).isoformat(), 'content_type': 'application/zip',
        })(),
    ]
    return client, fake_storage, manager


def test_stats_page_and_summary_api(tmp_path):
    client, _fake_storage, _manager = stats_client(tmp_path)
    page = client.get('/stats')
    assert page.status_code == 200
    assert '类型占比' in page.text
    assert '/static/js/stats.js' in page.text

    response = client.get('/api/stats/summary')
    assert response.status_code == 200
    summary = response.json()['summary']
    assert summary['object_count'] == 3
    assert summary['total_bytes'] == 1000
    assert summary['recent_7d_bytes'] == 400
    assert [item['type'] for item in summary['type_distribution']] == ['image', 'document', 'archive', 'other']


def test_stats_prefix_and_refresh_api(tmp_path):
    client, _fake_storage, _manager = stats_client(tmp_path)
    response = client.post('/api/stats/refresh?prefix=docs')
    assert response.status_code == 200
    payload = response.json()
    assert payload['message'] == '存储统计已刷新'
    assert payload['summary']['prefix'] == 'docs/'
    assert payload['summary']['object_count'] == 1
    assert payload['summary']['total_bytes'] == 300


def test_stats_api_requires_login(tmp_path):
    client, _fake_storage, _manager = stats_client(tmp_path)
    client.cookies.clear()
    assert client.get('/api/stats/summary').status_code == 401
    assert client.post('/api/stats/refresh').status_code == 401
