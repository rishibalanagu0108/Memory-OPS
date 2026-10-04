# Memory-ops TypeScript SDK

```typescript
import { MemoryOpsClient } from "@memory-ops/sdk";

const client = new MemoryOpsClient("http://localhost:8000", "api-token");
const result = await client.remember(
  "30000000-0000-0000-0000-000000000001",
  "30000000-0000-0000-0000-000000000010",
  {
    subject_id: "30000000-0000-0000-0000-000000000100",
    semantic_type: "preference",
    statement: "The user prefers concise answers.",
    purpose: "assistant_context",
  },
  "remember-001",
);
```

The client supports remember, inspect, list, and operation-status requests.
