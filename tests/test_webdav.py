import base64
import xml.etree.ElementTree as ET

import pytest

from app.security import hash_password
from app.webdav import basic_auth_matches, build_multistatus, href_for, map_path, parse_destination, relative_from_key


def test_basic_auth_rule_verifies_hash_without_exposing_password():
    encoded = base64.b64encode(b'dav-user:dav-password').decode()
    password_hash = hash_password('dav-password')
    assert basic_auth_matches(f'Basic {encoded}', username='dav-user', password_hash=password_hash) is True
    assert basic_auth_matches(f'Basic {encoded}', username='wrong', password_hash=password_hash) is False
    assert basic_auth_matches('Basic malformed', username='dav-user', password_hash=password_hash) is False
    assert basic_auth_matches('', username='dav-user', password_hash=password_hash) is False


def test_path_mapping_applies_prefix_root_and_rejects_traversal():
    mapped = map_path('docs/report.pdf', prefix_root='team-assets')
    assert mapped.key == 'team-assets/docs/report.pdf'
    assert mapped.relative == 'docs/report.pdf'
    assert mapped.collection_hint is False
    assert relative_from_key(mapped.key, prefix_root='team-assets/') == 'docs/report.pdf'
    assert href_for('docs/reports', collection=True) == '/webdav/docs/reports/'
    with pytest.raises(ValueError, match='上级目录'):
        map_path('../secret', prefix_root='team-assets')


def test_move_destination_stays_inside_current_endpoint():
    mapped = parse_destination(
        'http://example.test/webdav/archive/report.pdf',
        current_netloc='example.test',
        prefix_root='team-assets',
    )
    assert mapped.key == 'team-assets/archive/report.pdf'
    with pytest.raises(ValueError, match='当前 WebDAV'):
        parse_destination(
            'http://other.test/webdav/report.pdf',
            current_netloc='example.test',
            prefix_root='',
        )


def test_multistatus_builder_emits_collection_and_file_properties():
    payload = build_multistatus(
        [
            {'href': '/webdav/', 'collection': True, 'name': 'Bucket'},
            {
                'href': '/webdav/a.txt', 'collection': False, 'name': 'a.txt',
                'size': 3, 'content_type': 'text/plain', 'etag': 'etag-a',
            },
        ]
    )
    root = ET.fromstring(payload)
    responses = root.findall('{DAV:}response')
    assert len(responses) == 2
    assert responses[0].find('.//{DAV:}collection') is not None
    assert responses[1].find('.//{DAV:}getcontentlength').text == '3'
    assert responses[1].find('.//{DAV:}getcontenttype').text == 'text/plain'
