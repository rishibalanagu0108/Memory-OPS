"""Small production runtime boundary for readiness, quotas, and draining."""

from threading import Lock
from typing import Literal

from sqlalchemy import text

from memory_ops.persistence import TenantDatabase
from memory_ops.security import SecurityBoundary


Admission = Literal["admitted", "quota", "draining"]


def database_ready(database: TenantDatabase) -> bool:
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


class RuntimeGuard:
    """Bound one process and expose only safe dependency readiness."""

    def __init__(
        self,
        database: TenantDatabase,
        security: SecurityBoundary,
        max_in_flight: int,
    ) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be positive")
        self.database = database
        self.security = security
        self.max_in_flight = max_in_flight
        self._in_flight = 0
        self._draining = False
        self._lock = Lock()

    def admit(self) -> Admission:
        with self._lock:
            if self._draining:
                return "draining"
            if self._in_flight >= self.max_in_flight:
                return "quota"
            self._in_flight += 1
            return "admitted"

    def release(self) -> None:
        with self._lock:
            if self._in_flight:
                self._in_flight -= 1

    def start_draining(self) -> None:
        with self._lock:
            self._draining = True

    def ready(self) -> bool:
        with self._lock:
            if self._draining:
                return False
        if self.security.credential_resolver is None or self.security.policy is None:
            return False
        if not database_ready(self.database):
            return False
        with self._lock:
            return not self._draining
