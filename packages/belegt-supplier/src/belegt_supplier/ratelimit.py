# Spec: 08 §7.2 速率限制
"""进程内滑动窗口限流（单实例/自托管足够；多副本部署可替换为 Redis 实现，接口相同）。"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

TOKEN_PER_MINUTE = 60
TOKEN_UPLOADS_PER_DAY = 20
TOKEN_SUBMITS_TOTAL = 10
UNKNOWN_TOKEN_PER_IP_PER_MINUTE = 10
MAGIC_LINK_PER_EMAIL_PER_HOUR = 3
OTP_MAX_FAILURES = 5
OTP_LOCK_SECONDS = 15 * 60


class RateLimiter:
    def __init__(self, clock=time.time) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._penalty: dict[str, tuple[float, int]] = {}  # key -> (blocked_until, strikes)
        self._lock = threading.Lock()
        self.clock = clock

    def hit(self, key: str, limit: int, window_s: float, *, backoff: bool = False) -> float | None:
        """记录一次访问；超限返回需等待秒数（retry_after），否则 None。backoff=True 时超限后指数退避。"""
        now = self.clock()
        with self._lock:
            until, strikes = self._penalty.get(key, (0.0, 0))
            if until > now:
                return until - now
            q = self._hits[key]
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                wait = q[0] + window_s - now
                if backoff:
                    strikes += 1
                    wait = max(wait, window_s * (2 ** (strikes - 1)))
                    self._penalty[key] = (now + wait, strikes)
                return max(wait, 0.001)
            q.append(now)
            return None

    def count(self, key: str, window_s: float) -> int:
        now = self.clock()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_s:
                q.popleft()
            return len(q)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._penalty.clear()
