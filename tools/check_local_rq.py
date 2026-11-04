"""Check that a named local RQ worker registered on its expected queue."""

from __future__ import annotations

import os
import sys

from redis import Redis
from rq import Worker


def main(name: str, queue: str) -> int:
    connection = Redis.from_url(os.environ["REDIS_URL"])
    for worker in Worker.all(connection=connection):
        if worker.name == name and queue in {item.name for item in worker.queues}:
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
