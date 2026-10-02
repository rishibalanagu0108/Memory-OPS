"""Privacy-safe audit metadata for API and worker operations."""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID


AuditOutcome = Literal["allowed", "denied", "succeeded", "failed", "degraded"]
AUDIT_FIELDS = frozenset(
    {
        "occurred_at",
        "event_type",
        "outcome",
        "request_id",
        "tenant_id",
        "principal_id",
        "resource_type",
        "resource_id",
        "policy_version",
        "error_code",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_type: str
    outcome: AuditOutcome
    request_id: UUID
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tenant_id: UUID | None = None
    principal_id: UUID | None = None
    resource_type: str | None = None
    resource_id: UUID | None = None
    policy_version: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"allowed", "denied", "succeeded", "failed", "degraded"}:
            raise ValueError("unsupported audit outcome")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        for name in ("event_type", "resource_type", "policy_version", "error_code"):
            value = getattr(self, name)
            if value is not None and not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} must be a bounded identifier")

    def as_record(self) -> dict[str, str]:
        values = {
            "occurred_at": self.occurred_at.isoformat(),
            "event_type": self.event_type,
            "outcome": self.outcome,
            "request_id": str(self.request_id),
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "principal_id": str(self.principal_id) if self.principal_id else None,
            "resource_type": self.resource_type,
            "resource_id": str(self.resource_id) if self.resource_id else None,
            "policy_version": self.policy_version,
            "error_code": self.error_code,
        }
        return {key: value for key, value in values.items() if value is not None}


def emit_audit(logger: logging.Logger, event: AuditEvent) -> None:
    """Emit only the fixed, content-free audit schema."""

    logger.info("security_audit", extra={"audit": event.as_record()})
