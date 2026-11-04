"""Bounded-memory file inputs. Service methods still accept bytes for callers/tests."""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from .limits import MAX_UPLOAD, MAX_UPLOAD_MIB
from .models import WorkflowError


def size(content):
    return content.stat().st_size if isinstance(content, Path) else len(content)


def digest(content, suffix=b''):
    hashed = hashlib.sha256()
    if isinstance(content, Path):
        with content.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                hashed.update(chunk)
    else:
        hashed.update(content)
    hashed.update(suffix)
    return hashed.hexdigest()


def persist(path, content):
    if isinstance(content, Path):
        shutil.copyfile(content, path)
    else:
        path.write_bytes(content)


async def receive(file, request):
    if not (file.filename or '').lower().endswith('.xlsx'):
        raise WorkflowError('当前流程支持 .xlsx 文件。', 415)
    directory = request.state.upload_directory
    with tempfile.NamedTemporaryFile(dir=directory, suffix='.xlsx', delete=False) as handle:
        path = Path(handle.name)
        count = 0
        try:
            while chunk := await file.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_UPLOAD:
                    raise WorkflowError(f'上传文件不能超过 {MAX_UPLOAD_MIB} MiB。', 413)
                # File parsing has already spooled multipart data to disk; copy bounded chunks.
                from starlette.concurrency import run_in_threadpool
                await run_in_threadpool(handle.write, chunk)
        except BaseException:
            handle.close()
            path.unlink(missing_ok=True)
            raise
    return path
