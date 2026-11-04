"""Request-scoped execution events. Future agents can emit the same event contract."""
import asyncio
import time
import uuid
from datetime import datetime, timezone


class Trace:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.queue = asyncio.Queue()
        self.started = time.monotonic()

    def emit(self, actor, status, message, **extra):
        self.queue.put_nowait(dict(request_id=self.id, actor=actor, actor_type="module",
                                  parent_id=None, status=status, message=message,
                                  timestamp=datetime.now(timezone.utc).isoformat(),
                                  elapsed_ms=round((time.monotonic()-self.started)*1000), **extra))
