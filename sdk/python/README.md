# Memory-ops Python SDK

```python
from uuid import UUID

from memory_ops_sdk import MemoryOpsClient, RememberMemoryRequest

with MemoryOpsClient("http://localhost:8000", "api-token") as client:
    result = client.remember(
        UUID("30000000-0000-0000-0000-000000000001"),
        UUID("30000000-0000-0000-0000-000000000010"),
        RememberMemoryRequest(
            subject_id=UUID("30000000-0000-0000-0000-000000000100"),
            semantic_type="preference",
            statement="The user prefers concise answers.",
            purpose="assistant_context",
        ),
        idempotency_key="remember-001",
    )
```

The client supports remember, inspect, list, and operation-status requests.
