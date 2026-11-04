"""Check this container's own RQ worker, not just Redis availability."""
import os
import socket
from datetime import UTC, datetime

from redis import Redis
from rq import Worker


def main():
    connection = Redis.from_url(os.environ['REDIS_URL'], socket_timeout=5, socket_connect_timeout=5)
    worker = Worker.find_by_key('rq:worker:' + socket.gethostname(), connection=connection)
    if worker is None or worker.last_heartbeat is None:
        raise SystemExit(1)
    heartbeat = worker.last_heartbeat
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    raise SystemExit(0 if (datetime.now(UTC) - heartbeat).total_seconds() < 120 else 1)


if __name__ == '__main__':
    main()
