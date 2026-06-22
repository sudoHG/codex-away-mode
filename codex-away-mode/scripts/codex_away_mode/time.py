from datetime import datetime, timedelta, timezone
from typing import Protocol
import time as time_module


class Clock(Protocol):
    def now(self) -> datetime:
        ...

    def sleep(self, seconds: float) -> None:
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time_module.sleep(seconds)


class FakeClock:
    def __init__(self, current):
        self.current = current

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current = self.current + timedelta(seconds=seconds)
