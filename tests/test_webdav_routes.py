import base64
import xml.etree.ElementTree as ET


def dav_auth(username='dav-user', password='webdav-password'):
    encoded = base64.b64encode(f'{username}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {encoded}'}


def webdav_client(tmp_path, *, read_only=False, prefix_root='', trash_enabled=False):
    from tests.test_upload_routes import make_client
    from tests.test_settings import update_payload

    client, fake_storage, manager = make_client(tmp_path)
    fake_storage.object_entries = [
        type('Obj', (), {
            'name': 'docs/', 'size': 0, 'etag': 'docs',
            'time_created': '2026-07-26T10:00:00+00:00', 'content_type': 'application/x-directory',
        })(),
        type('Obj', (), {
            'name': 'docs/a.txt', 'size': 5, 'etag': 'a',
            'time_created': '2026-07-26T10:00:00+00:00', 'content_type': 'text/plain',
        })(),
        type('Obj', (), {
            'name': 'root.txt', 'size': 4, 'etag': 'root',
            'time_created': '2026-07-26T10:00:00+00:00', 'content_type': 'text/plain',
        })(),
    ]
    fake_storage.download_payloads = {
        'docs/a.txt': b'hello',
        'root.txt': b'root',
    }
    payload = update_payload(read_only=read_only, trash_enabled=trash_enabled)
    payload['storage']['prefix_root'] = prefix_root
    saved = client.post('/api/settings', json=payload)
    assert saved.status_code == 200, saved.text
    return client, fake_storage, manager


def test_webdav_is_not_exposed_until_enabled(tmp_path):
    from tests.test_upload_routes import make_client

    client, _fake_storage, _manager = make_client(tmp_path)
    response = client.request('OPTIONS', '/webdav/', headers=dav_auth())
    assert response.status_code == 404
    assert response.json()['detail'] == 'WebDAV 未启用'


def test_webdav_auth_options_and_propfind(tmp_path):
    client, _fake_storage, _manager = webdav_client(tmp_path)
    no_auth = client.request('OPTIONS', '/webdav/')
    assert no_auth.status_code == 401
    assert 'Basic' in no_auth.headers['www-authenticate']

    options = client.request('OPTIONS', '/webdav/', headers=dav_auth())
    assert options.status_code == 200
    assert 'PROPFIND' in options.headers['allow']
    assert options.headers['dav'] == '1, 2'

    propfind = client.request('PROPFIND', '/webdav/', headers={**dav_auth(), 'Depth': '1'})
    assert propfind.status_code == 207
    assert propfind.headers['content-type'].startswith('application/xml')
    root = ET.fromstring(propfind.content)
    hrefs = [node.text for node in root.findall('{DAV:}response/{DAV:}href')]
    assert '/webdav/' in hrefs
    assert '/webdav/docs/' in hrefs
    assert '/webdav/root.txt' in hrefs


def test_webdav_get_put_mkcol_delete_and_move(tmp_path):
    client, fake_storage, _manager = webdav_client(tmp_path)
    headers = dav_auth()

    downloaded = client.get('/webdav/docs/a.txt', headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == b'hello'
    assert downloaded.headers['content-disposition'].startswith('attachment;')

    created = client.request('PUT', '/webdav/new.txt', headers={**headers, 'Content-Type': 'text/plain'}, content=b'new')
    assert created.status_code == 201
    assert fake_storage.single_uploads[-1][0] == 'new.txt'
    updated = client.request('PUT', '/webdav/new.txt', headers={**headers, 'Content-Type': 'text/plain'}, content=b'newer')
    assert updated.status_code == 204

    mkcol = client.request('MKCOL', '/webdav/new-folder', headers=headers)
    assert mkcol.status_code == 201
    assert fake_storage.single_uploads[-1][0] == 'new-folder/'

    moved = client.request(
        'MOVE',
        '/webdav/docs/a.txt',
        headers={**headers, 'Destination': 'http://testserver/webdav/docs/b.txt', 'Overwrite': 'F'},
    )
    assert moved.status_code == 201
    assert fake_storage.single_uploads[-1][0] == 'docs/b.txt'
    assert 'docs/a.txt' in fake_storage.deleted_objects

    deleted = client.request('DELETE', '/webdav/docs/b.txt', headers=headers)
    assert deleted.status_code == 204
    assert 'docs/b.txt' in fake_storage.deleted_objects


def test_webdav_read_only_blocks_all_write_methods_but_allows_reads(tmp_path):
    client, fake_storage, _manager = webdav_client(tmp_path, read_only=True)
    headers = dav_auth()
    assert client.get('/webdav/root.txt', headers=headers).status_code == 200
    assert client.request('PUT', '/webdav/blocked.txt', headers=headers, content=b'x').status_code == 403
    assert client.request('DELETE', '/webdav/root.txt', headers=headers).status_code == 403
    assert client.request('MKCOL', '/webdav/blocked/', headers=headers).status_code == 403
    assert client.request('MOVE', '/webdav/root.txt', headers={**headers, 'Destination': '/webdav/moved.txt'}).status_code == 403
    assert fake_storage.deleted_objects == []


def test_webdav_prefix_root_maps_client_root_and_rejects_traversal(tmp_path):
    client, fake_storage, _manager = webdav_client(tmp_path, prefix_root='team-assets')
    fake_storage.object_entries = [
        type('Obj', (), {
            'name': 'team-assets/a.txt', 'size': 3, 'etag': 'a',
            'time_created': '2026-07-26T10:00:00+00:00', 'content_type': 'text/plain',
        })(),
    ]
    fake_storage.download_payloads = {'team-assets/a.txt': b'abc'}
    headers = dav_auth()
    response = client.get('/webdav/a.txt', headers=headers)
    assert response.status_code == 200
    assert response.content == b'abc'

    traversal = client.get('/webdav/..%2Fsecret', headers=headers)
    assert traversal.status_code == 400


def test_webdav_delete_uses_shared_recycle_bin_policy(tmp_path):
    client, fake_storage, _manager = webdav_client(tmp_path, trash_enabled=True)
    response = client.request('DELETE', '/webdav/root.txt', headers=dav_auth())
    assert response.status_code == 204
    trash_upload = fake_storage.single_uploads[-1]
    assert trash_upload[0].startswith('.trash/')
    assert trash_upload[0].endswith('/root.txt')
    assert trash_upload[1] == b'root'
    assert fake_storage.deleted_objects == ['root.txt']

def test_webdav_auth_failures_are_rate_limited(tmp_path):
    client, _fake_storage, _manager = webdav_client(tmp_path)
    responses = [
        client.get('/webdav', headers=dav_auth(password='wrong-password'))
        for _ in range(6)
    ]

    assert [response.status_code for response in responses[:4]] == [401] * 4
    assert responses[4].status_code == 429
    assert responses[5].status_code == 429
    assert int(responses[4].headers['retry-after']) > 0
    from app.routes import WEBDAV_FAILURE_LIMITER
    WEBDAV_FAILURE_LIMITER.reset('webdav:testclient')
