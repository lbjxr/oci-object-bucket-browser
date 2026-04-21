from __future__ import annotations

from io import BytesIO
from typing import BinaryIO

import oci
from oci.config import validate_config
from oci.exceptions import ConfigFileNotFound, InvalidConfig, ServiceError
from oci.object_storage import ObjectStorageClient

from app.config import Settings, get_settings
from app.models import ObjectEntry, PreviewData
from app.utils import guess_content_type, is_image_type, is_pdf_type, is_text_type


class OCIStorageError(RuntimeError):
    pass


class OCIStorageService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.namespace or not self.settings.bucket_name:
            raise OCIStorageError("OCI_NAMESPACE 和 OCI_BUCKET_NAME 必填")
        try:
            config = oci.config.from_file(self.settings.oci_config_path, self.settings.oci_profile)
            validate_config(config)
        except (ConfigFileNotFound, InvalidConfig, ValueError, KeyError) as exc:
            raise OCIStorageError(f"OCI 配置加载失败: {exc}") from exc
        self._client = ObjectStorageClient(config)

    @property
    def client(self) -> ObjectStorageClient:
        return self._client

    @property
    def namespace(self) -> str:
        return self.settings.namespace

    @property
    def bucket_name(self) -> str:
        return self.settings.bucket_name

    def list_objects(self, prefix: str = "") -> list[ObjectEntry]:
        try:
            response = self.client.list_objects(
                self.namespace,
                self.bucket_name,
                prefix=prefix or None,
                fields="name,size,etag,timeCreated,md5",
                limit=self.settings.max_list_limit,
            )
        except ServiceError as exc:
            raise OCIStorageError(f"列出对象失败: {exc.message}") from exc
        entries = []
        for item in response.data.objects:
            entries.append(
                ObjectEntry(
                    name=item.name,
                    size=item.size,
                    etag=item.etag,
                    time_created=item.time_created.isoformat() if item.time_created else None,
                    content_type=guess_content_type(item.name),
                )
            )
        return entries

    def upload_file(self, object_name: str, fileobj: BinaryIO, content_type: str | None = None) -> None:
        body = fileobj.read()
        content_type = guess_content_type(object_name, content_type)
        try:
            self.client.put_object(
                self.namespace,
                self.bucket_name,
                object_name,
                body,
                content_type=content_type,
            )
        except ServiceError as exc:
            raise OCIStorageError(f"上传失败: {exc.message}") from exc

    def get_object(self, object_name: str):
        try:
            return self.client.get_object(self.namespace, self.bucket_name, object_name)
        except ServiceError as exc:
            raise OCIStorageError(f"下载失败: {exc.message}") from exc

    def get_preview(self, object_name: str) -> PreviewData:
        response = self.get_object(object_name)
        content_type = guess_content_type(object_name, response.headers.get("content-type"))
        payload = response.data.content

        if is_text_type(content_type):
            text = payload[: self.settings.preview_text_limit].decode("utf-8", errors="replace")
            return PreviewData(kind="text", content_type=content_type, text=text)
        if is_image_type(content_type):
            return PreviewData(kind="image", content_type=content_type, bytes_data=payload)
        if is_pdf_type(content_type):
            return PreviewData(kind="pdf", content_type=content_type, bytes_data=payload)
        return PreviewData(kind="download", content_type=content_type, download_only=True)

    def open_stream(self, object_name: str) -> tuple[BytesIO, str]:
        response = self.get_object(object_name)
        content_type = guess_content_type(object_name, response.headers.get("content-type"))
        return BytesIO(response.data.content), content_type


__all__ = ["OCIStorageService", "OCIStorageError"]
