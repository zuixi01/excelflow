"""Offline PostgreSQL + data-directory backup/restore. Never stops live services itself."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def database_environment(url):
    parsed = make_url(url)
    if not parsed.drivername.startswith('postgresql'):
        raise ValueError('production backup requires PostgreSQL')
    env = dict(os.environ)
    env.update(PGHOST=parsed.host or '', PGPORT=str(parsed.port or 5432), PGDATABASE=parsed.database or '',
               PGUSER=parsed.username or '', PGPASSWORD=parsed.password or '')
    for name in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey'):
        if name in parsed.query:
            env['PG' + name.upper()] = str(parsed.query[name])
    return env


def pg(command, url):
    try:
        subprocess.run(command, env=database_environment(url), check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError('PostgreSQL backup/restore command failed; verify client version, TLS and access. No backup marked complete.') from None


def verify(bundle):
    manifest = json.loads((bundle / 'manifest.json').read_text('utf-8'))
    if set(manifest.get('sha256', {})) != {'database.dump', 'data.tar'}:
        raise ValueError('invalid backup manifest')
    for name, checksum in manifest['sha256'].items():
        if sha(bundle / name) != checksum:
            raise ValueError('backup checksum mismatch: ' + name)
    return manifest


def extract_data(archive, target):
    root = target.resolve()
    with tarfile.open(archive, 'r') as handle:
        members = handle.getmembers()
        for member in members:
            resolved = (root / member.name).resolve()
            if not resolved.is_relative_to(root) or not (member.isfile() or member.isdir()):
                raise ValueError('unsafe file in backup archive')
        for member in members:
            path = root / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'verify', 'restore'])
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--services-stopped', action='store_true', help='Required acknowledgement: all writers are stopped, including workers and recovery')
    args = parser.parse_args()
    if args.action == 'verify':
        verify(args.bundle); print('backup checksums verified'); return
    if not args.services_stopped or args.data_root is None:
        parser.error('stop all writers first, then supply --services-stopped and --data-root')
    url = os.environ.get('DATABASE_URL')
    if not url:
        parser.error('DATABASE_URL is required in the environment')
    root, bundle = args.data_root.resolve(), args.bundle.resolve()
    if bundle.is_relative_to(root) or root.is_relative_to(bundle):
        parser.error('backup and data directories must be separate')
    if args.action == 'backup':
        if not root.is_dir():
            parser.error('data directory does not exist')
        bundle.mkdir(parents=True, exist_ok=False)
        bundle.chmod(0o700)
        pg(['pg_dump', '--format=custom', '--no-owner', '--file', str(bundle / 'database.dump')], url)
        with tarfile.open(bundle / 'data.tar', 'w') as handle:
            for path in root.rglob('*'):
                if path.is_symlink():
                    raise ValueError('data directory must not contain symbolic links')
                handle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        manifest = {'format': 1, 'sha256': {name: sha(bundle / name) for name in ['database.dump', 'data.tar']}}
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        verify(bundle)
        print('offline backup complete; copy bundle to encrypted off-host storage')
        return
    verify(bundle)
    if root.exists() and any(root.iterdir()):
        parser.error('restore requires an empty new data directory')
    engine = create_engine(url)
    try:
        if inspect(engine).get_table_names():
            parser.error('restore requires an empty new database')
    finally:
        engine.dispose()
    root.mkdir(parents=True, exist_ok=True)
    # Validate/extract before restoring the database. A failed restore stays offline.
    extract_data(bundle / 'data.tar', root)
    pg(['pg_restore', '--exit-on-error', '--single-transaction', '--no-owner', '--dbname', database_environment(url)['PGDATABASE'], str(bundle / 'database.dump')], url)
    print('restore complete in new targets; validate counts, file hashes, permissions and sample exports before switching traffic')


if __name__ == '__main__':
    main()
