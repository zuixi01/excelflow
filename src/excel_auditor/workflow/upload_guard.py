from __future__ import annotations

import tempfile
from functools import lru_cache

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .capacity import ResourceLease, disk_check, limit
from .limits import MAX_TOTAL_UPLOAD
from .models import WorkflowError
from .repository import WorkflowStore


@lru_cache(maxsize=4)
def guard_store(root, engine):
    return WorkflowStore(root / 'workflow', engine)


async def guarded_request(request, call_next, root, engine):
    if 'multipart/form-data' not in request.headers.get('content-type', '').lower():
        if request.method in {'POST', 'PUT', 'PATCH'}:
            maximum = limit('MAX_JSON_MIB', 8) * 1024**2
            try:
                declared = int(request.headers.get('content-length', '0'))
            except ValueError:
                return JSONResponse({'detail': '无效的请求长度。'}, status_code=400)
            if declared < 0 or declared > maximum:
                return JSONResponse({'detail': '请求内容过大，请分批保存。'}, status_code=413)
            original_receive = request._receive
            received = 0
            async def bounded_json():
                nonlocal received
                message = await original_receive()
                received += len(message.get('body', b''))
                if received > maximum:
                    raise HTTPException(413, '请求内容过大，请分批保存。')
                return message
            request._receive = bounded_json
        return await call_next(request)
    # Acquire before FastAPI parses multipart, including legacy upload routes.
    store = await run_in_threadpool(guard_store, root, engine)
    lease = ResourceLease(store, 'upload', limit('MAX_UPLOADS', 16))
    try:
        await run_in_threadpool(disk_check, root)
        maximum = MAX_TOTAL_UPLOAD + 8 * 1024 * 1024
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            raise WorkflowError('无效的上传长度。', 400)
        if length < 0 or length > maximum:
            raise WorkflowError('上传请求超过容量上限。', 413)
        await run_in_threadpool(lease.acquire)
    except WorkflowError as exc:
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=exc.status,
                            headers={'Retry-After': '10'} if exc.status == 429 else {})
    staging = store.root / 'staging'
    staging.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='upload-', dir=staging) as directory:
            request.state.upload_directory = directory
            original_receive = request._receive
            received = 0
            async def bounded_receive():
                nonlocal received
                lease.check()
                message = await original_receive()
                received += len(message.get('body', b''))
                if received > maximum:
                    raise HTTPException(413, '上传请求超过容量上限。')
                return message
            request._receive = bounded_receive
            return await call_next(request)
    finally:
        await run_in_threadpool(lease.close)
