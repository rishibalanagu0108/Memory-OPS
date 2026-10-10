"""Synchronous typed client for the Memory-ops API."""

from types import TracebackType
from typing import Self, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel

from memory_ops_sdk.models import (
    CorrectMemoryRequest,
    CorrectMemoryResult,
    ForgetMemoryResult,
    Memory,
    MemoryList,
    OperationStatus,
    RememberMemoryRequest,
    RememberMemoryResult,
)


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class MemoryOpsError(Exception):
    """Safe structured error returned by the Memory-ops API."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MemoryOpsClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip() or not token:
            raise ValueError("base_url and token are required")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "memory-ops-python/0.1.0",
            },
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _prefix(tenant_id: UUID, workspace_id: UUID) -> str:
        return f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}"

    @staticmethod
    def _result(response: httpx.Response, model: type[ResponseModel]) -> ResponseModel:
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                body = {}
            code = body.get("code", "http_error") if isinstance(body, dict) else "http_error"
            message = (
                body.get("message", "request failed")
                if isinstance(body, dict)
                else "request failed"
            )
            raise MemoryOpsError(response.status_code, str(code), str(message))
        return model.model_validate(response.json())

    def remember(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        request: RememberMemoryRequest,
        *,
        idempotency_key: str,
    ) -> RememberMemoryResult:
        response = self._http.post(
            f"{self._prefix(tenant_id, workspace_id)}/memories",
            headers={"Idempotency-Key": idempotency_key},
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._result(response, RememberMemoryResult)

    def inspect(
        self, tenant_id: UUID, workspace_id: UUID, memory_id: UUID
    ) -> Memory:
        response = self._http.get(
            f"{self._prefix(tenant_id, workspace_id)}/memories/{memory_id}"
        )
        return self._result(response, Memory)

    def correct(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        memory_id: UUID,
        request: CorrectMemoryRequest,
        *,
        idempotency_key: str,
    ) -> CorrectMemoryResult:
        response = self._http.post(
            f"{self._prefix(tenant_id, workspace_id)}/memories/{memory_id}/corrections",
            headers={"Idempotency-Key": idempotency_key},
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._result(response, CorrectMemoryResult)

    def forget(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        memory_id: UUID,
        subject_id: UUID,
        *,
        idempotency_key: str,
        agent_id: UUID | None = None,
    ) -> ForgetMemoryResult:
        params = {"subject_id": str(subject_id)}
        if agent_id is not None:
            params["agent_id"] = str(agent_id)
        response = self._http.delete(
            f"{self._prefix(tenant_id, workspace_id)}/memories/{memory_id}",
            headers={"Idempotency-Key": idempotency_key},
            params=params,
        )
        return self._result(response, ForgetMemoryResult)

    def list_memories(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        subject_id: UUID | None = None,
        purpose: str | None = None,
        limit: int = 100,
    ) -> MemoryList:
        params = {"limit": limit}
        if subject_id is not None:
            params["subject_id"] = str(subject_id)
        if purpose is not None:
            params["purpose"] = purpose
        response = self._http.get(
            f"{self._prefix(tenant_id, workspace_id)}/memories", params=params
        )
        return self._result(response, MemoryList)

    def operation_status(
        self, tenant_id: UUID, workspace_id: UUID, operation_id: UUID
    ) -> OperationStatus:
        response = self._http.get(
            f"{self._prefix(tenant_id, workspace_id)}/operations/{operation_id}"
        )
        return self._result(response, OperationStatus)
