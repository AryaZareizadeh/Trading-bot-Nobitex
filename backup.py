"""Verified recovery bundles of stopped runtimes; secrets remain separate."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import zipfile

from runtime_lease import FileLease

FORMAT_VERSION = 1
MAX_BUNDLE_BYTES = 32 * 1024 ** 3


def _safe_member(name):
    if not isinstance(name, str):
        raise ValueError('Unsafe archive member path')
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or any(part in {'', '.', '..'} for part in name.split('/'))
            or any(ord(character) < 32 for character in name)):
        raise ValueError('Unsafe archive member path')
    for part in path.parts:
        if (part.endswith(('.', ' ')) or any(char in '<>"|?*' for char in part)
                or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', part.split('.')[0], re.I)):
            raise ValueError('Unsafe archive member path')
    return path


def _digest(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _required_artifacts(root):
    for database in root.rglob('trader.sqlite3'):
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='records'").fetchone():
                continue
            for model_id, raw in conn.execute("SELECT key,value FROM records WHERE namespace='trained_models'"):
                record = json.loads(raw)
                if record.get('status') != 'ready':
                    continue
                if len(model_id) != 32 or any(c not in '0123456789abcdef' for c in model_id):
                    raise ValueError('Invalid registered model artifact identity')
                # Use the catalog so future architectures share one artifact contract.
                from trainer_catalog import artifact_suffix
                suffix = artifact_suffix(record.get('architecture', 'alexnet1d-v1'))
                if not (database.parent / 'models' / (model_id + suffix)).is_file():
                    raise ValueError('A registered model checkpoint artifact is missing')
            for namespace, raw in conn.execute("SELECT namespace,value FROM records WHERE "
                    "namespace='spot_experiments' OR (namespace='spot_manager' AND key='research')"):
                report = json.loads(raw)
                if not isinstance(report, dict):
                    continue
                references = [report.get('model_artifact'), (report.get('data_snapshot') or {}).get('file')]
                if namespace == 'spot_manager' and report.get('paper_model'):
                    references.append('spot_research_model.joblib')
                for reference in filter(None, references):
                    member = _safe_member(reference)
                    if len(member.parts) != 1 or not reference.startswith('spot_'):
                        raise ValueError('Invalid registered spot artifact path')
                    path = database.parent / reference
                    if not path.is_file():
                        raise ValueError('A registered spot research artifact is missing')
                    if reference == report.get('model_artifact') and report.get('model_file_sha256'):
                        with path.open('rb') as stream:
                            if _digest(stream)[0] != report['model_file_sha256']:
                                raise ValueError('Registered spot model artifact hash mismatch')


def backup_bundle(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError('Runtime source directory does not exist')
    if output.exists():
        raise ValueError('Backup destination already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(FileLease(source / '.dashboard.lock'))
        databases = sorted(source.rglob('*.sqlite3'))
        if not databases:
            raise ValueError('No runtime databases found')
        for database in databases:
            stack.enter_context(FileLease(str(database) + '.lock'))
        # Freeze candidate paths before staging, which may be inside the source
        # volume when the operator stores its bundle under data/backups.
        candidates = list(source.rglob('*'))
        staging = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.bundle-', dir=output.parent)))
        data = staging / 'data'
        data.mkdir()
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            _safe_member(relative.as_posix())
            database = path.suffix == '.sqlite3'
            artifact = ('models' in relative.parts and path.suffix in {'.pt', '.joblib'}) or (
                path.name.startswith('spot_') and (path.suffix in {'.joblib', '.json'} or path.name.endswith('.json.gz')))
            if not (database or artifact):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ValueError('Runtime artifact path escapes its directory')
            destination = data / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if database:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as original:
                    with closing(sqlite3.connect(destination)) as target:
                        original.backup(target)
            else:
                shutil.copyfile(path, destination)
        _required_artifacts(data)
        files = []
        for path in sorted(data.rglob('*')):
            if path.is_file():
                with path.open('rb') as stream:
                    digest, size = _digest(stream)
                files.append(dict(path=path.relative_to(data).as_posix(), sha256=digest, size=size))
        manifest = dict(format_version=FORMAT_VERSION, created_at=datetime.now(timezone.utc).isoformat(),
                        requires_separate_master_key=(data / 'accounts.sqlite3').exists(), files=files)
        archive_path = staging / 'bundle.zip'
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest, indent=2))
            for item in files:
                archive.write(data / item['path'], item['path'])
        verify_bundle(archive_path)
        # Exclusive creation prevents overwriting a concurrently created backup.
        with output.open('xb') as destination, archive_path.open('rb') as stream:
            try:
                shutil.copyfileobj(stream, destination)
                destination.flush()
                os.fsync(destination.fileno())
            except BaseException:
                destination.close()
                output.unlink(missing_ok=True)
                raise
    return manifest


def _manifest(archive):
    members = archive.infolist()
    names = [member.filename for member in members]
    if len(names) != len({name.casefold() for name in names}) or 'manifest.json' not in names:
        raise ValueError('Invalid archive member manifest')
    for member in members:
        _safe_member(member.filename)
        if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
            raise ValueError('Unsupported archive member type')
    if sum(member.file_size for member in members) > MAX_BUNDLE_BYTES:
        raise ValueError('Archive exceeds the supported size')
    if archive.getinfo('manifest.json').file_size > 16 * 1024 * 1024:
        raise ValueError('Oversized archive manifest')
    try:
        manifest = json.loads(archive.read('manifest.json'))
        if manifest['format_version'] != FORMAT_VERSION or not isinstance(manifest['files'], list):
            raise ValueError('Unsupported archive manifest')
        paths = [item['path'] for item in manifest['files']]
        if len(paths) != len(set(paths)) or set(paths) | {'manifest.json'} != set(names):
            raise ValueError('Archive members do not match manifest')
        for item in manifest['files']:
            _safe_member(item['path'])
            if (type(item['size']) is not int or item['size'] < 0
                    or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
                raise ValueError('Invalid archive integrity manifest')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid archive manifest') from exc
    return manifest


def verify_bundle(source):
    try:
        with zipfile.ZipFile(source) as archive:
            manifest = _manifest(archive)
            for item in manifest['files']:
                if archive.getinfo(item['path']).file_size != item['size']:
                    raise ValueError('Archive size integrity check failed')
                with archive.open(item['path']) as stream:
                    digest, size = _digest(stream)
                if digest != item['sha256'] or size != item['size']:
                    raise ValueError('Archive hash integrity check failed')
            return manifest
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Archive integrity verification failed') from exc


def restore_bundle(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Restore destination must be a new or empty directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'data'
        staging.mkdir()
        with zipfile.ZipFile(source) as archive:
            manifest = _manifest(archive)
            for item in manifest['files']:
                path = staging.joinpath(*_safe_member(item['path']).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item['path']) as original, path.open('xb') as target:
                    digest, size = hashlib.sha256(), 0
                    while chunk := original.read(1024 * 1024):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    if size != item['size'] or digest.hexdigest() != item['sha256']:
                        raise ValueError('Archive hash integrity check failed during restore')
        for database in staging.rglob('*.sqlite3'):
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
                if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Restored database integrity check failed')
        _required_artifacts(staging)
        if destination.exists():
            destination.rmdir()  # Refuses if another process populated it.
        staging.rename(destination)
    return manifest
