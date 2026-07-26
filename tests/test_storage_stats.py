from datetime import datetime, timezone

from app.storage_stats import stat_type_for, summarize_objects


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def obj(name, size, content_type, created):
    return type('Object', (), {
        'name': name,
        'size': size,
        'content_type': content_type,
        'time_created': created,
    })()


def test_stat_type_maps_supported_categories():
    assert stat_type_for('image/png', 'a.png') == 'image'
    assert stat_type_for('application/pdf', 'a.pdf') == 'document'
    assert stat_type_for('text/plain', 'a.txt') == 'document'
    assert stat_type_for('application/zip', 'a.zip') == 'archive'
    assert stat_type_for('application/octet-stream', 'a.bin') == 'other'


def test_summary_excludes_folder_placeholders_and_calculates_recent_distribution():
    summary = summarize_objects(
        [
            obj('docs/', 0, 'application/x-directory', '2026-07-26T10:00:00+00:00'),
            obj('a.png', 100, 'image/png', '2026-07-25T10:00:00+00:00'),
            obj('b.pdf', 300, 'application/pdf', '2026-07-20T10:00:00+00:00'),
            obj('c.zip', 600, 'application/zip', '2026-07-01T10:00:00+00:00'),
        ],
        now=NOW,
        prefix='team/',
    )
    assert summary['prefix'] == 'team/'
    assert summary['object_count'] == 3
    assert summary['total_bytes'] == 1000
    assert summary['recent_7d_bytes'] == 400
    assert summary['recent_7d_object_count'] == 2
    assert [(item['type'], item['count'], item['bytes'], item['percent']) for item in summary['type_distribution']] == [
        ('image', 1, 100, 10.0),
        ('document', 1, 300, 30.0),
        ('archive', 1, 600, 60.0),
        ('other', 0, 0, 0.0),
    ]
