import json
import zipfile

import pytest

from storage import Store


def runtime(root):
    store = Store(root / 'trader.sqlite3')
    model_id = 'a' * 32
    store.put('trained_models', model_id, dict(id=model_id, status='ready', architecture='alexnet1d-v1'))
    store.put('bots', 'saved', dict(id='saved', mode='simulation'))
    (root / 'models').mkdir()
    (root / 'models' / (model_id + '.pt')).write_bytes(b'isolated checkpoint content')
    (root / '.env').write_text('operator secret excluded')
    return store


def test_bundle_restores_database_artifacts_and_excludes_secrets(tmp_path):
    from backup import backup_bundle, restore_bundle, verify_bundle
    source = tmp_path / 'source'
    runtime(source)
    bundle = tmp_path / 'backup.zip'
    backup_bundle(source, bundle)
    assert verify_bundle(bundle)['format_version'] == 1
    destination = tmp_path / 'restored'
    restore_bundle(bundle, destination)
    restored = Store(destination / 'trader.sqlite3')
    assert restored.get('bots', 'saved')['mode'] == 'simulation'
    assert (destination / 'models' / ('a' * 32 + '.pt')).read_bytes() == b'isolated checkpoint content'
    assert not (destination / '.env').exists()
    assert not list(destination.rglob('*.lock'))
    with pytest.raises(ValueError, match='empty|exist'):
        restore_bundle(bundle, destination)


def test_bundle_rejects_missing_registered_checkpoint_and_active_runtime(tmp_path):
    from backup import backup_bundle
    source = tmp_path / 'source'
    store = runtime(source)
    store.acquire_runtime()
    try:
        with pytest.raises((ValueError, RuntimeError), match='active|stop'):
            backup_bundle(source, tmp_path / 'active.zip')
    finally:
        store.release_runtime()
    (source / 'models' / ('a' * 32 + '.pt')).unlink()
    with pytest.raises(ValueError, match='checkpoint|artifact'):
        backup_bundle(source, tmp_path / 'missing.zip')
    assert not (tmp_path / 'missing.zip').exists()


def test_bundle_checks_hashes_and_rejects_archive_path_escape(tmp_path):
    from backup import backup_bundle, restore_bundle, verify_bundle
    source = tmp_path / 'source'
    runtime(source)
    bundle = tmp_path / 'backup.zip'
    backup_bundle(source, bundle)
    corrupt = tmp_path / 'corrupt.zip'
    with zipfile.ZipFile(bundle) as original, zipfile.ZipFile(corrupt, 'w') as target:
        for member in original.infolist():
            value = original.read(member.filename)
            if member.filename.endswith('.pt'):
                value = b'corrupted checkpoint content'
            target.writestr(member.filename, value)
    with pytest.raises(ValueError, match='hash|size|integrity'):
        verify_bundle(corrupt)
    malicious = tmp_path / 'malicious.zip'
    with zipfile.ZipFile(malicious, 'w') as archive:
        archive.writestr('../escaped', 'bad')
        archive.writestr('manifest.json', json.dumps({'format_version': 1, 'files': []}))
    with pytest.raises(ValueError, match='path|member|manifest'):
        restore_bundle(malicious, tmp_path / 'destination')
    assert not (tmp_path / 'escaped').exists()
    assert not (tmp_path / 'destination').exists()


def test_multiuser_bundle_contains_private_stores_and_candle_cache(tmp_path):
    from backup import backup_bundle, restore_bundle
    import sqlite3
    source = tmp_path / 'source'
    runtime(source / 'users' / ('b' * 32))
    with sqlite3.connect(source / 'accounts.sqlite3') as connection:
        connection.execute('CREATE TABLE users(id TEXT)')
        connection.execute('INSERT INTO users VALUES(?)', ('b' * 32,))
    with sqlite3.connect(source / 'candles.sqlite3') as connection:
        connection.execute('CREATE TABLE candles(open_time INTEGER)')
        connection.execute('INSERT INTO candles VALUES(1)')
    bundle = tmp_path / 'bundle.zip'
    backup_bundle(source, bundle)
    destination = tmp_path / 'restored'
    restore_bundle(bundle, destination)
    assert (destination / 'accounts.sqlite3').is_file()
    assert (destination / 'candles.sqlite3').is_file()
    assert (destination / 'users' / ('b' * 32) / 'models' / ('a' * 32 + '.pt')).is_file()
