from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from math import ceil

PBKDF2_ITERATIONS = 240_000

class FailureRateLimiter:
    def __init__(self, *, max_failures: int = 5, window_seconds: int = 300) -> None:
        if max_failures <= 0 or window_seconds <= 0:
            raise ValueError("限流参数必须大于 0")
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, int]:
        current = time.monotonic() if now is None else now
        with self._lock:
            failures = self._failures.get(key)
            if not failures:
                return False, 0
            cutoff = current - self.window_seconds
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                self._failures.pop(key, None)
                return False, 0
            if len(failures) < self.max_failures:
                return False, 0
            return True, max(1, ceil(failures[0] + self.window_seconds - current))

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            failures = self._failures.setdefault(key, deque())
            cutoff = current - self.window_seconds
            while failures and failures[0] <= cutoff:
                failures.popleft()
            failures.append(current)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


    def clear(self) -> None:
        with self._lock:
            self._failures.clear()

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_text),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False
