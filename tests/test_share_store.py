import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.share_store import ShareAccessError, ShareStore, public_share, share_status, summarize_shares


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_share_store_hashes_token_and_password_and_persists(tmp_path):
    store = ShareStore(str(tmp_path / 'shares.json'))
    record, token = store.create(
        object_key='docs/report.pdf',
        expires_at=NOW + timedelta(days=3),
        download_limit=5,
        password='share-password',
        now=NOW,
    )

    raw_text = (tmp_path / 'shares.json').read_text(encoding='utf-8')
    raw = json.loads(raw_text)
    assert token not in raw_text
    assert 'share-password' not in raw_text
    assert raw['shares'][0]['token_hash'] == record['token_hash']
    assert raw['shares'][0]['password_hash'].startswith('pbkdf2_sha256$')

    reloaded = ShareStore(str(tmp_path / 'shares.json'))
    resolved = reloaded.require_active(token, now=NOW)
    assert resolved['id'] == record['id']
    assert reloaded.password_matches(resolved, 'share-password') is True
    assert reloaded.password_matches(resolved, 'wrong') is False

    public = public_share(resolved, now=NOW)
    assert public['password_protected'] is True
    assert public['remaining_downloads'] == 5
    assert 'token_hash' not in public
    assert 'password_hash' not in public


def test_share_status_handles_expiry_revocation_and_limit(tmp_path):
    store = ShareStore(str(tmp_path / 'shares.json'))
    active, active_token = store.create(
        object_key='active.txt',
        expires_at=NOW + timedelta(hours=2),
        download_limit=1,
        now=NOW,
    )
    expired, expired_token = store.create(
        object_key='expired.txt',
        expires_at=NOW - timedelta(seconds=1),
        download_limit=None,
        now=NOW - timedelta(days=1),
    )

    assert share_status(active, now=NOW) == 'active'
    assert share_status(expired, now=NOW) == 'expired'
    with pytest.raises(ShareAccessError, match='已过期'):
        store.require_active(expired_token, now=NOW)

    store.reserve_download(active['id'], now=NOW)
    with pytest.raises(ShareAccessError, match='次数已用完'):
        store.require_active(active_token, now=NOW)

    revoked, revoked_token = store.create(
        object_key='revoked.txt',
        expires_at=NOW + timedelta(days=1),
        download_limit=None,
        now=NOW,
    )
    store.revoke(revoked['id'], now=NOW)
    with pytest.raises(ShareAccessError, match='已撤销'):
        store.require_active(revoked_token, now=NOW)


def test_download_limit_reservation_is_atomic(tmp_path):
    store = ShareStore(str(tmp_path / 'shares.json'))
    record, _token = store.create(
        object_key='once.bin',
        expires_at=NOW + timedelta(days=1),
        download_limit=1,
        now=NOW,
    )

    def reserve():
        try:
            store.reserve_download(record['id'], now=NOW)
            return 'reserved'
        except ShareAccessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: reserve(), range(2)))

    assert sorted(results) == ['exhausted', 'reserved']
    assert store.get(record['id'])['download_count'] == 1


def test_share_summary_uses_today_accesses_and_active_expiry_window(tmp_path):
    store = ShareStore(str(tmp_path / 'shares.json'))
    soon, _ = store.create(
        object_key='soon.txt',
        expires_at=NOW + timedelta(hours=3),
        download_limit=None,
        now=NOW,
    )
    later, _ = store.create(
        object_key='later.txt',
        expires_at=NOW + timedelta(days=5),
        download_limit=None,
        now=NOW,
    )
    store.record_access(soon['id'], now=NOW)
    store.record_access(soon['id'], now=NOW)
    store.record_access(later['id'], now=NOW - timedelta(days=1))

    summary = summarize_shares(store.list_records(), now=NOW)
    assert summary == {
        'active_count': 2,
        'today_access_count': 2,
        'expiring_soon_count': 1,
        'total_count': 2,
    }
