import assert from "node:assert/strict";
import test from "node:test";

import {
  MemoryOpsClient,
  MemoryOpsError,
  type EvidenceReference,
  type FetchLike,
  type Memory,
} from "../src/index.js";

const tenant = "30000000-0000-0000-0000-000000000001";
const workspace = "30000000-0000-0000-0000-000000000010";
const subject = "30000000-0000-0000-0000-000000000100";
const memoryId = "30000000-0000-0000-0000-000000000300";
const versionId = "30000000-0000-0000-0000-000000000400";
const operationId = "30000000-0000-0000-0000-000000000500";

const memory: Memory = {
  memory_id: memoryId,
  version_id: versionId,
  version_number: 1,
  tenant_id: tenant,
  workspace_id: workspace,
  subject_id: subject,
  agent_id: null,
  semantic_type: "preference",
  lifecycle: "active",
  statement: "The user prefers concise answers.",
  normalized_subject: "user",
  normalized_predicate: "prefers",
  normalized_value: "concise answers",
  qualifiers: {},
  valid_from: "2026-10-05T10:00:00Z",
  valid_to: null,
  recorded_at: "2026-10-05T10:00:00Z",
  sensitivity: "normal",
  lifetime: "durable",
  origin: "explicit",
  purpose: "assistant_context",
  policy_version: "local-test-token-v1",
  access_scope: [],
  retention_until: null,
  evidence: [
    {
      evidence_type: "conversation_message",
      reference_id: "message-1",
      locator: null,
    },
  ],
};

test("typed client covers supported memory operations", async () => {
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  const fetcher: FetchLike = async (input, init) => {
    const url = String(input);
    requests.push({ url, init });
    if (init?.method === "POST" || init?.method === "DELETE") {
      return Response.json({
        memory_id: memoryId,
        version_id: versionId,
        operation_id: operationId,
        operation_status: "pending",
        replayed: false,
      }, { status: 201 });
    }
    if (url.endsWith(`/memories/${memoryId}`)) {
      return Response.json(memory);
    }
    if (url.includes("/memories?")) {
      return Response.json({ items: [memory] });
    }
    return Response.json({
      operation_id: operationId,
      status: "pending",
      attempts: 0,
      last_error_code: null,
    });
  };
  const client = new MemoryOpsClient(
    "https://memory.example/", "test-token", 10_000, fetcher,
  );

  const remembered = await client.remember(tenant, workspace, {
    subject_id: subject,
    semantic_type: "preference",
    statement: memory.statement,
    purpose: "assistant_context",
    evidence: memory.evidence,
  }, "remember-1");
  const inspected = await client.inspect(tenant, workspace, memoryId);
  const corrected = await client.correct(tenant, workspace, memoryId, {
    subject_id: subject,
    statement: "The user prefers light mode.",
  }, "correct-1");
  const listed = await client.listMemories(tenant, workspace, {
    subjectId: subject,
    purpose: "assistant_context",
    limit: 10,
  });
  const operation = await client.operationStatus(tenant, workspace, operationId);
  const forgotten = await client.forget(
    tenant, workspace, memoryId, subject, "forget-1",
  );

  assert.equal(remembered.memory_id, memoryId);
  assert.equal(inspected.statement, memory.statement);
  assert.equal(corrected.memory_id, memoryId);
  assert.deepEqual(listed.items, [inspected]);
  assert.equal(operation.status, "pending");
  assert.equal(forgotten.memory_id, memoryId);
  assert.ok(requests.every(({ init }) =>
    new Headers(init?.headers).get("Authorization") === "Bearer test-token"));
  assert.equal(new Headers(requests[0].init?.headers).get("Idempotency-Key"), "remember-1");
  assert.match(requests[2].url, /\/corrections$/);
  assert.equal(new Headers(requests[2].init?.headers).get("Idempotency-Key"), "correct-1");
  assert.match(requests[3].url, /limit=10/);
  assert.match(requests[3].url, new RegExp(`subject_id=${subject}`));
  assert.equal(requests[5].init?.method, "DELETE");
  assert.match(requests[5].url, new RegExp(`subject_id=${subject}`));
});

test("client returns safe structured API errors", async () => {
  const denied: FetchLike = async () => Response.json(
    { code: "unauthenticated", message: "authentication required" },
    { status: 401 },
  );
  const client = new MemoryOpsClient(
    "https://memory.example", "bad-token", 10_000, denied,
  );

  await assert.rejects(
    client.inspect(tenant, workspace, memoryId),
    (error: unknown) => {
      assert.ok(error instanceof MemoryOpsError);
      assert.equal(error.statusCode, 401);
      assert.equal(error.code, "unauthenticated");
      assert.equal(error.message, "unauthenticated: authentication required");
      return true;
    },
  );
});

const invalidEvidence: EvidenceReference = {
  // @ts-expect-error unsupported evidence types must fail typechecking
  evidence_type: "manual_sdk_test",
  reference_id: "message-1",
};
void invalidEvidence;
