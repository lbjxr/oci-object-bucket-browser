from pathlib import Path


def test_server_proxy_upload_init_stage_commit_and_status(tmp_path):
    from tests.test_upload_routes import make_client

    client, _fake_storage, manager = make_client(tmp_path)

    init = client.post(
        '/api/server-uploads/init',
        json={
            'filename': 'movie.mkv',
            'file_size': 10 * 1024 * 1024,
            'content_type': 'video/x-matroska',
            'file_fingerprint': 'movie.mkv::10485760::video/x-matroska::1',
        },
    )
    assert init.status_code == 200
    init_payload = init.json()
    assert init_payload['ok'] is True
    assert init_payload['strategy'] == 'oci-multipart-server-proxy'
    assert init_payload['proxy_chunk_size'] == 8 * 1024 * 1024
    assert init_payload['uploaded_chunks'] == []
    assert init_payload['missing_chunks'] == [0, 1]
    temp_upload_id = init_payload['temp_upload_id']

    part1 = client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=0&chunk_sha256={"ad97f87076920684e2ca66fc44e5d322797dc9d64706b174e51b5d0828937043"}',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert part1.status_code == 200
    assert part1.json()['stored_bytes'] == 8 * 1024 * 1024
    assert part1.json()['already_uploaded'] is False

    resumed = client.post(
        '/api/server-uploads/init',
        json={
            'filename': 'movie.mkv',
            'file_size': 10 * 1024 * 1024,
            'content_type': 'video/x-matroska',
            'file_fingerprint': 'movie.mkv::10485760::video/x-matroska::1',
        },
    )
    assert resumed.status_code == 200
    resumed_payload = resumed.json()
    assert resumed_payload['reused'] is True
    assert resumed_payload['uploaded_chunks'] == [0]
    assert resumed_payload['missing_chunks'] == [1]

    status_before_commit = client.get(f'/api/server-uploads/staging/{temp_upload_id}')
    assert status_before_commit.status_code == 200
    assert status_before_commit.json()['uploaded_chunks'] == [0]
    assert status_before_commit.json()['missing_chunks'] == [1]

    skip_same_chunk = client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=0&chunk_sha256={"ad97f87076920684e2ca66fc44e5d322797dc9d64706b174e51b5d0828937043"}',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert skip_same_chunk.status_code == 200
    assert skip_same_chunk.json()['already_uploaded'] is True

    part2 = client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=1&chunk_sha256={"85a6e0cdf20bfbc76abca53afb39fdf2edd59ac8fcf236ee730d8ea2851ca975"}',
        content=b'b' * (2 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert part2.status_code == 200

    commit = client.post(
        f'/api/server-uploads/commit?temp_upload_id={temp_upload_id}',
        json={
            'filename': 'movie.mkv',
            'file_size': 10 * 1024 * 1024,
            'content_type': 'video/x-matroska',
        },
    )
    assert commit.status_code == 200
    commit_payload = commit.json()
    assert commit_payload['task_id'] == 'task-1'
    assert commit_payload['message'] == '文件已上传到服务器，后台入桶任务已创建'

    status_after_commit = client.get(f'/api/server-uploads/staging/{temp_upload_id}')
    assert status_after_commit.status_code == 200
    assert status_after_commit.json()['committed'] is True
    assert manager.created
    filename, content_type, staged_path, total_size = manager.created[0]
    assert filename == 'movie.mkv'
    assert content_type == 'video/x-matroska'
    assert total_size == 10 * 1024 * 1024
    assert Path(staged_path).exists()
    assert Path(staged_path).stat().st_size == 10 * 1024 * 1024

    listed = client.get('/api/server-uploads/tasks')
    assert listed.status_code == 200
    tasks = listed.json()['tasks']
    assert len(tasks) == 1
    assert tasks[0]['task_id'] == 'task-1'
    assert tasks[0]['progress'] == 0.0
    assert tasks[0]['current_phase'] == 'waiting'
    assert tasks[0]['phase_label'] == '等待执行'
    assert tasks[0]['recovered'] is False
    assert tasks[0]['status_label'] == '排队中'
    assert tasks[0]['is_retrying'] is False
    assert tasks[0]['retry_count'] == 0
    assert tasks[0]['retry_attempt'] == 0
    assert tasks[0]['retry_max_attempts'] == 0
    assert tasks[0]['retry_kind'] is None
    assert tasks[0]['retry_part_num'] is None
    assert tasks[0]['retry_label'] is None
    assert tasks[0]['last_error'] is None

    status = client.get('/api/server-uploads/tasks/task-1')
    assert status.status_code == 200
    assert status.json()['task_id'] == 'task-1'
    assert status.json()['current_phase'] == 'waiting'
    assert status.json()['is_retrying'] is False
    assert status.json()['last_error'] is None


def test_server_proxy_upload_rejects_conflicting_duplicate_chunk(tmp_path):
    from tests.test_upload_routes import make_client

    client, _fake_storage, _manager = make_client(tmp_path)
    init = client.post(
        '/api/server-uploads/init',
        json={
            'filename': 'movie.mkv',
            'file_size': 10 * 1024 * 1024,
            'content_type': 'video/x-matroska',
            'file_fingerprint': 'movie.mkv::10485760::video/x-matroska::2',
        },
    )
    temp_upload_id = init.json()['temp_upload_id']

    first = client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=0&chunk_sha256={"ad97f87076920684e2ca66fc44e5d322797dc9d64706b174e51b5d0828937043"}',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert first.status_code == 200

    second = client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=0&chunk_sha256={"50eafc14df6613ee151196ea55fec40a1811bfd06b06aadfd33f200247219004"}',
        content=b'c' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert second.status_code == 409
    assert '内容不一致' in second.json()['detail']


def test_server_proxy_upload_init_rejects_existing_object_without_overwrite(tmp_path):
    from tests.test_upload_routes import make_client

    client, fake_storage, _manager = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'movie.mkv', 'size': 10, 'etag': 'etag-movie', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'video/x-matroska'})(),
    ]
    response = client.post(
        '/api/server-uploads/init',
        json={
            'filename': 'movie.mkv',
            'file_size': 10 * 1024 * 1024,
            'content_type': 'video/x-matroska',
            'file_fingerprint': 'movie.mkv::10485760::video/x-matroska::conflict',
        },
    )
    assert response.status_code == 409
    payload = response.json()
    assert payload['conflict']['action'] == 'upload'
    assert payload['conflict']['destination_path'] == 'movie.mkv'



def test_server_proxy_upload_commit_allows_overwrite_after_confirmation(tmp_path):
    from tests.test_upload_routes import make_client

    client, fake_storage, manager = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {'name': 'movie.mkv', 'size': 10, 'etag': 'etag-movie', 'time_created': '2026-04-22T10:00:00+00:00', 'content_type': 'video/x-matroska'})(),
    ]
    init = client.post(
        '/api/server-uploads/init',
        json={
            'filename': 'movie-new.mkv',
            'file_size': 10 * 1024 * 1024,
            'content_type': 'video/x-matroska',
            'file_fingerprint': 'movie-new.mkv::10485760::video/x-matroska::ok',
        },
    )
    temp_upload_id = init.json()['temp_upload_id']
    client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=0&chunk_sha256={"ad97f87076920684e2ca66fc44e5d322797dc9d64706b174e51b5d0828937043"}',
        content=b'a' * (8 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=1&chunk_sha256={"85a6e0cdf20bfbc76abca53afb39fdf2edd59ac8fcf236ee730d8ea2851ca975"}',
        content=b'b' * (2 * 1024 * 1024),
        headers={'Content-Type': 'application/octet-stream'},
    )
    manager.created.clear()
    conflict_commit = client.post(
        f'/api/server-uploads/commit?temp_upload_id={temp_upload_id}',
        json={'filename': 'movie-new.mkv', 'file_size': 10 * 1024 * 1024, 'content_type': 'video/x-matroska'},
    )
    assert conflict_commit.status_code == 200
    overwrite_commit = client.post(
        f'/api/server-uploads/commit?temp_upload_id={temp_upload_id}',
        json={'filename': 'movie-new.mkv', 'file_size': 10 * 1024 * 1024, 'content_type': 'video/x-matroska', 'overwrite': True},
    )
    assert overwrite_commit.status_code == 200
    assert overwrite_commit.json()['task_id'] == 'task-1'



def test_server_proxy_commit_returns_immediate_background_ack(tmp_path):
    from tests.test_upload_routes import make_client

    client, _fake_storage, _manager = make_client(tmp_path)
    init = client.post(
        '/api/server-uploads/init',
        json={
            'filename': 'tiny.txt',
            'file_size': 3,
            'content_type': 'text/plain',
            'file_fingerprint': 'tiny.txt::3::text/plain::ack',
        },
    )
    temp_upload_id = init.json()['temp_upload_id']

    client.put(
        f'/api/server-uploads/staging/{temp_upload_id}?chunk_index=0&chunk_sha256={"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}',
        content=b'abc',
        headers={'Content-Type': 'application/octet-stream'},
    )

    commit = client.post(
        f'/api/server-uploads/commit?temp_upload_id={temp_upload_id}',
        json={
            'filename': 'tiny.txt',
            'file_size': 3,
            'content_type': 'text/plain',
        },
    )
    assert commit.status_code == 200
    payload = commit.json()
    assert payload['ok'] is True
    assert payload['status'] == 'queued'
    assert payload['phase'] == 'waiting'
    assert payload['message'] == '文件已上传到服务器，后台入桶任务已创建'



def test_server_proxy_task_cancel_endpoint(tmp_path):
    from tests.test_upload_routes import make_client

    client, _fake_storage, manager = make_client(tmp_path)
    response = client.delete('/api/server-uploads/tasks/task-1')
    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['status'] == 'canceled'
    assert manager.task.status == 'canceled'
