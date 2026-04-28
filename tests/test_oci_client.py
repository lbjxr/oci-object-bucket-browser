from types import SimpleNamespace

from app.oci_client import OCIStorageService


class _Part:
    def __init__(self, part_number, etag):
        self.part_number = part_number
        self.etag = etag


class _FakeClient:
    def __init__(self):
        self.calls = []

    def list_multipart_upload_parts(self, namespace, bucket_name, object_name, multipart_upload_id, limit=1000, page=None):
        self.calls.append((namespace, bucket_name, object_name, multipart_upload_id, limit, page))
        return SimpleNamespace(
            data=[
                _Part(1, 'etag-1'),
                _Part(2, 'etag-2'),
                _Part(3, 'etag-3'),
            ],
            headers={},
        )


def test_list_multipart_uploaded_parts_accepts_list_payload_with_part_number(monkeypatch):
    service = OCIStorageService.__new__(OCIStorageService)
    service.settings = SimpleNamespace(namespace='ns', bucket_name='bucket')
    service._client = _FakeClient()

    parts = service.list_multipart_uploaded_parts(
        object_name='big.bin',
        multipart_upload_id='mp-1',
    )

    assert parts == {1: 'etag-1', 2: 'etag-2', 3: 'etag-3'}
    assert service.client.calls == [
        ('ns', 'bucket', 'big.bin', 'mp-1', 1000, None)
    ]
