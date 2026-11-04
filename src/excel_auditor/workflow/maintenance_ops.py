"""Read-only health reporting and conservative temporary-artifact cleanup."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from .capacity import limit, stamp, serialize
from .repository import executions, objects, resource_leases


def health(store):
    with store.engine.connect() as con:
        counts = dict(con.execute(select(executions.c.status, func.count()).group_by(executions.c.status)).all())
        oldest = con.execute(select(func.min(executions.c.created_at)).where(executions.c.status.in_(['pending', 'queued']))).scalar_one_or_none()
        leases = dict(con.execute(select(resource_leases.c.resource, func.count()).where(resource_leases.c.expires_at > stamp()).group_by(resource_leases.c.resource)).all())
        expired = con.execute(select(func.count()).select_from(executions).where(executions.c.status == 'running', executions.c.lease_until <= stamp())).scalar_one()
    disk = shutil.disk_usage(store.root)
    waiting = counts.get('pending', 0) + counts.get('queued', 0)
    return {'executions': counts, 'resource_leases': leases, 'expired_running': expired,
            'oldest_wait_seconds': max(0, (datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds()) if oldest else 0,
            'disk_free_bytes': disk.free, 'disk_total_bytes': disk.total,
            'accepting': disk.free >= limit('MIN_FREE_DISK_MIB', 2048) * 1024**2 and waiting < limit('WORKFLOW_MAX_WAITING', 50)}


def cleanup(store, *, days=7, apply=False):
    if days < 1:
        raise ValueError('retention must be at least one day')
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with store.engine.begin() as con:
        serialize(con, store.engine)
        active = select(objects.c.id).join(executions, executions.c.object_id == objects.c.id).where(executions.c.status.in_(['pending', 'queued', 'running']))
        # Skip all cleanup while work is outstanding. Prefer retained files to deleting an in-use preview.
        if con.execute(active.limit(1)).first():
            return {'skipped': 'active_work', 'objects': [], 'applied': False}
        candidates = list(con.execute(select(objects.c.id).where(objects.c.kind.in_(['return', 'nameconfirmation', 'operation']),
            objects.c.updated_at < cutoff, objects.c.status.not_in(['processing', 'learning']))).scalars())
        if apply and candidates:
            con.execute(delete(executions).where(executions.c.object_id.in_(candidates)))
            con.execute(delete(objects).where(objects.c.id.in_(candidates)))
        uploads_active = con.execute(select(resource_leases.c.token).where(resource_leases.c.resource == 'upload', resource_leases.c.expires_at > stamp()).limit(1)).first()
        orphan_paths = []
        files_root = store.root / 'files'
        if files_root.exists():
            for path in files_root.iterdir():
                if (path.is_dir() and re.fullmatch(r'(task|template|export|return|op|nameconfirmation)_[a-f0-9]+', path.name)
                        and datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat() < cutoff
                        and not con.execute(select(objects.c.id).where(objects.c.id == path.name)).first()):
                    orphan_paths.append(path)
    paths = [store.root / 'files' / name for name in candidates]
    paths.extend(orphan_paths)
    staging = store.root / 'staging'
    if staging.exists() and not uploads_active:
        paths.extend(path for path in staging.iterdir() if path.is_dir() and datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat() < cutoff)
    if apply:
        root = store.root.resolve()
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('cleanup target outside workflow root')
            if path.exists():
                shutil.rmtree(path)
    return {'objects': candidates, 'orphan_directories': len(orphan_paths), 'directories': len(paths), 'applied': apply}


def main():
    import os
    from pathlib import Path
    from .execution import open_store
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['health', 'cleanup'])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--days', type=int, default=7)
    args = parser.parse_args()
    store = open_store(Path(os.environ.get('EXCEL_AUDITOR_DATA', 'var')) / 'workflow')
    try:
        print(json.dumps(health(store) if args.action == 'health' else cleanup(store, days=args.days, apply=args.apply), ensure_ascii=False))
    finally:
        store.engine.dispose()


if __name__ == '__main__':
    main()
