import random
import time
from dataclasses import dataclass, field


class TransientError(Exception):
    pass


@dataclass
class ExponentialBackoff:
    max_attempts: int = 4
    base: float = 1.0
    max_delay: float = 30.0
    jitter: bool = True

    def __iter__(self):
        for attempt in range(1, self.max_attempts + 1):
            delay = min(self.base * (2 ** (attempt - 1)), self.max_delay)
            if self.jitter:
                delay *= 0.5 + random.random() * 0.5
            yield attempt, delay
            if attempt < self.max_attempts:
                time.sleep(delay)


@dataclass
class CircuitBreaker:
    max_failures: int = 5
    cooldown: float = 300.0
    _failures: dict[str, int] = field(default_factory=dict)
    _tripped_until: dict[str, float] = field(default_factory=dict)

    def record_failure(self, backend_name: str):
        self._failures[backend_name] = self._failures.get(backend_name, 0) + 1
        if self._failures[backend_name] >= self.max_failures:
            self._tripped_until[backend_name] = time.time() + self.cooldown

    def record_success(self, backend_name: str):
        self._failures.pop(backend_name, None)
        self._tripped_until.pop(backend_name, None)

    def is_tripped(self, backend_name: str) -> bool:
        until = self._tripped_until.get(backend_name, 0)
        if until == 0:
            return False
        if time.time() >= until:
            self._tripped_until.pop(backend_name, None)
            self._failures.pop(backend_name, None)
            return False
        return True

    def failure_count(self, backend_name: str) -> int:
        return self._failures.get(backend_name, 0)