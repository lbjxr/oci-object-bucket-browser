from __future__ import annotations

import base64
import hmac
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

from app.security import verify_password


DAV_NAMESPACE = "DAV:"
ET.register_namespace("", DAV_NAMESPACE)


@dataclass(frozen=True)
class WebDAVPath:
    key: str
    relative: str
    collection_hint: bool


def basic_auth_matches(header: str, *, username: str, password_hash: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        supplied_username, separator, supplied_password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return bool(
        separator
        and hmac.compare_digest(supplied_username, username)
        and password_hash
        and verify_password(supplied_password, password_hash)
    )


def map_path(path: str | None, *, prefix_root: str = "") -> WebDAVPath:
    decoded = unquote(path or "").replace("\\", "/")
    collection_hint = not decoded or decoded.endswith("/")
    parts: list[str] = []
    for part in decoded.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("WebDAV 路径不允许上级目录")
        parts.append(part)
    relative = "/".join(parts)
    normalized_root = prefix_root.strip().strip("/")
    if normalized_root:
        normalized_root += "/"
    key = f"{normalized_root}{relative}" if relative else normalized_root
    return WebDAVPath(key=key, relative=relative, collection_hint=collection_hint)


def relative_from_key(key: str, *, prefix_root: str = "") -> str:
    normalized_root = prefix_root.strip().strip("/")
    if normalized_root:
        normalized_root += "/"
    if normalized_root and key.startswith(normalized_root):
        return key[len(normalized_root):].strip("/")
    return key.strip("/")


def href_for(relative: str, *, collection: bool) -> str:
    encoded = quote(relative.strip("/"), safe="/")
    href = "/webdav/" + encoded if encoded else "/webdav/"
    if collection and not href.endswith("/"):
        href += "/"
    return href


def parse_destination(destination: str, *, current_netloc: str, prefix_root: str = "") -> WebDAVPath:
    parsed = urlparse(destination)
    if parsed.netloc and parsed.netloc != current_netloc:
        raise ValueError("MOVE 目标必须位于当前 WebDAV 端点")
    target_path = parsed.path if parsed.path else destination
    prefix = "/webdav"
    if target_path == prefix:
        relative_path = ""
    elif target_path.startswith(prefix + "/"):
        relative_path = target_path[len(prefix) + 1:]
    else:
        raise ValueError("MOVE 目标必须位于 /webdav/ 下")
    return map_path(relative_path, prefix_root=prefix_root)


def build_multistatus(resources: list[dict]) -> bytes:
    multistatus = ET.Element(f"{{{DAV_NAMESPACE}}}multistatus")
    for resource in resources:
        response = ET.SubElement(multistatus, f"{{{DAV_NAMESPACE}}}response")
        ET.SubElement(response, f"{{{DAV_NAMESPACE}}}href").text = resource["href"]
        propstat = ET.SubElement(response, f"{{{DAV_NAMESPACE}}}propstat")
        prop = ET.SubElement(propstat, f"{{{DAV_NAMESPACE}}}prop")
        ET.SubElement(prop, f"{{{DAV_NAMESPACE}}}displayname").text = resource["name"]
        resource_type = ET.SubElement(prop, f"{{{DAV_NAMESPACE}}}resourcetype")
        if resource.get("collection"):
            ET.SubElement(resource_type, f"{{{DAV_NAMESPACE}}}collection")
        if not resource.get("collection") and resource.get("size") is not None:
            ET.SubElement(prop, f"{{{DAV_NAMESPACE}}}getcontentlength").text = str(resource["size"])
        if not resource.get("collection") and resource.get("content_type"):
            ET.SubElement(prop, f"{{{DAV_NAMESPACE}}}getcontenttype").text = resource["content_type"]
        if resource.get("etag"):
            ET.SubElement(prop, f"{{{DAV_NAMESPACE}}}getetag").text = resource["etag"]
        if resource.get("modified"):
            ET.SubElement(prop, f"{{{DAV_NAMESPACE}}}getlastmodified").text = resource["modified"]
        ET.SubElement(propstat, f"{{{DAV_NAMESPACE}}}status").text = "HTTP/1.1 200 OK"
    return ET.tostring(multistatus, encoding="utf-8", xml_declaration=True)
