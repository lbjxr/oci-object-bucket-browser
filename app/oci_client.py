from __future__ import annotations

from io import BytesIO
from typing import BinaryIO
import socket

import oci
from oci.config import validate_config
from oci.exceptions import ConfigFileNotFound, InvalidConfig, ServiceError
from oci.object_storage import ObjectStorageClient
from oci.object_storage.models import (
    CommitMultipartUploadDetails,
    CommitMultipartUploadPartDetails,
    CreateMultipartUploadDetails,
)

from app.config import Settings, get_settings
from app.models import ObjectEntry, PreviewData
from app.utils import guess_content_type, is_image_type, is_pdf_type, is_text_type


class OCIStorageError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "unknown",
        retryable: bool = False,
        status_code: int = 500,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.reason = reason or message


def classify_upload_exception(exc: Exception) -> tuple[str, bool, int, str]:
    if isinstance(exc, ServiceError):
        status = int(getattr(exc, "status", 500) or 500)
        code = (getattr(exc, "code", "") or "").strip()
        message = (getattr(exc, "message", "") or str(exc)).strip() or "OCI 服务返回异常"
        if 500 <= status <= 599:
            return "http_5xx", True, 503, f"OCI 服务暂时不可用（HTTP {status}{f', {code}' if code else ''}）: {message}"
        if status == 408:
            return "timeout", True, 504, f"OCI 服务处理超时（HTTP 408）: {message}"
        if status == 429:
            return "http_4xx", True, 429, f"OCI 服务限流（HTTP 429）: {message}"
        if 400 <= status <= 499:
            return "http_4xx", False, status, f"OCI 服务拒绝该分片请求（HTTP {status}{f', {code}' if code else ''}）: {message}"
        return "unknown", False, 500, message

    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        return "timeout", True, 504, "上传分片到 OCI 超时"

    lowered = str(exc).lower()
    connection_keywords = (
        "connection reset",
        "connection aborted",
        "connection refused",
        "broken pipe",
        "temporarily unavailable",
        "remote end closed",
        "timed out",
        "timeout",
        "econnreset",
        "econnaborted",
        "econnrefused",
    )
    if any(keyword in lowered for keyword in connection_keywords):
        return "connection", True, 503, f"上传分片到 OCI 时连接中断: {exc}"

    return "unknown", False, 500, f"上传分片失败: {exc}"


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
        content_type = guess_content_type(object_name, content_type)
        try:
            if hasattr(fileobj, "seek"):
                fileobj.seek(0)
            self.client.put_object(
                self.namespace,
                self.bucket_name,
                object_name,
                fileobj,
                content_type=content_type,
            )
        except ServiceError as exc:
            raise OCIStorageError(f"上传失败: {exc.message}") from exc
        except Exception as exc:
            raise OCIStorageError(f"上传失败: {exc}") from exc

    def create_multipart_upload(self, object_name: str, content_type: str | None = None) -> str:
        content_type = guess_content_type(object_name, content_type)
        try:
            response = self.client.create_multipart_upload(
                self.namespace,
                self.bucket_name,
                CreateMultipartUploadDetails(
                    object=object_name,
                    content_type=content_type,
                ),
            )
            return response.data.upload_id
        except ServiceError as exc:
            raise OCIStorageError(f"创建分段上传失败: {exc.message}") from exc

    def upload_part(
        self,
        *,
        object_name: str,
        multipart_upload_id: str,
        part_num: int,
        payload: bytes,
        content_type: str | None = None,
    ) -> str:
        try:
            response = self.client.upload_part(
                self.namespace,
                self.bucket_name,
                object_name,
                multipart_upload_id,
                part_num,
                BytesIO(payload),
                content_length=len(payload),
            )
            etag = response.headers.get("etag") or getattr(response.data, "etag", None)
            if not etag:
                raise OCIStorageError("分片上传成功但未返回 ETag", category="unknown", retryable=False, status_code=500)
            return etag
        except OCIStorageError:
            raise
        except Exception as exc:
            category, retryable, status_code, reason = classify_upload_exception(exc)
            retry_hint = "可重试" if retryable else "不可重试"
            raise OCIStorageError(
                f"上传分片失败（part {part_num}，{retry_hint}，{category}）: {reason}",
                category=category,
                retryable=retryable,
                status_code=status_code,
                reason=reason,
            ) from exc

    def list_multipart_uploaded_parts(self, *, object_name: str, multipart_upload_id: str) -> dict[int, str]:
        page = None
        parts: dict[int, str] = {}
        try:
            while True:
                response = self.client.list_multipart_upload_parts(
                    self.namespace,
                    self.bucket_name,
                    object_name,
                    multipart_upload_id,
                    limit=1000,
                    page=page,
                )
                for item in response.data.parts:
                    parts[int(item.part_num)] = item.etag
                page = response.headers.get("opc-next-page")
                if not page:
                    break
            return parts
        except ServiceError as exc:
            raise OCIStorageError(f"查询已上传分片失败: {exc.message}") from exc

    def commit_multipart_upload(
        self,
        *,
        object_name: str,
        multipart_upload_id: str,
        parts: list[tuple[int, str]],
    ) -> None:
        try:
            self.client.commit_multipart_upload(
                self.namespace,
                self.bucket_name,
                object_name,
                multipart_upload_id,
                CommitMultipartUploadDetails(
                    parts_to_commit=[
                        CommitMultipartUploadPartDetails(part_num=part_num, etag=etag)
                        for part_num, etag in sorted(parts, key=lambda item: item[0])
                    ]
                ),
            )
        except ServiceError as exc:
            raise OCIStorageError(f"合并分段上传失败: {exc.message}") from exc

    def abort_multipart_upload(self, *, object_name: str, multipart_upload_id: str) -> None:
        try:
            self.client.abort_multipart_upload(
                self.namespace,
                self.bucket_name,
                object_name,
                multipart_upload_id,
            )
        except ServiceError as exc:
            raise OCIStorageError(f"取消分段上传失败: {exc.message}") from exc

    def delete_object(self, object_name: str) -> None:
        try:
            self.client.delete_object(self.namespace, self.bucket_name, object_name)
        except ServiceError as exc:
            raise OCIStorageError(f"删除失败: {exc.message}") from exc

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


__all__ = ["OCIStorageService", "OCIStorageError", "classify_upload_exception"]
