import type {
  Memory,
  MemoryList,
  OperationStatus,
  RememberMemoryRequest,
  RememberMemoryResult,
  UUID,
} from "./models.js";

export type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

export interface ListMemoryOptions {
  subjectId?: UUID;
  purpose?: string;
  limit?: number;
}

export class MemoryOpsError extends Error {
  constructor(
    readonly statusCode: number,
    readonly code: string,
    message: string,
  ) {
    super(`${code}: ${message}`);
    this.name = "MemoryOpsError";
  }
}

export class MemoryOpsClient {
  private readonly baseUrl: string;

  constructor(
    baseUrl: string,
    private readonly token: string,
    private readonly timeoutMs = 10_000,
    private readonly fetcher: FetchLike = globalThis.fetch,
  ) {
    if (!baseUrl.trim() || !token) {
      throw new TypeError("baseUrl and token are required");
    }
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  private prefix(tenantId: UUID, workspaceId: UUID): string {
    return `/v1/tenants/${tenantId}/workspaces/${workspaceId}`;
  }

  private async request<Result>(path: string, init: RequestInit = {}): Promise<Result> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("User-Agent", "memory-ops-typescript/0.1.0");
    if (init.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      ...init,
      headers,
      signal: init.signal ?? AbortSignal.timeout(this.timeoutMs),
    });
    const body: unknown = await response.json();
    if (!response.ok) {
      const error = body as { code?: unknown; message?: unknown };
      const code = typeof error?.code === "string" ? error.code : "http_error";
      const message =
        typeof error?.message === "string" ? error.message : "request failed";
      throw new MemoryOpsError(response.status, code, message);
    }
    return body as Result;
  }

  remember(
    tenantId: UUID,
    workspaceId: UUID,
    request: RememberMemoryRequest,
    idempotencyKey: string,
  ): Promise<RememberMemoryResult> {
    return this.request(`${this.prefix(tenantId, workspaceId)}/memories`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(request),
    });
  }

  inspect(tenantId: UUID, workspaceId: UUID, memoryId: UUID): Promise<Memory> {
    return this.request(
      `${this.prefix(tenantId, workspaceId)}/memories/${memoryId}`,
    );
  }

  listMemories(
    tenantId: UUID,
    workspaceId: UUID,
    options: ListMemoryOptions = {},
  ): Promise<MemoryList> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 100) });
    if (options.subjectId !== undefined) {
      query.set("subject_id", options.subjectId);
    }
    if (options.purpose !== undefined) {
      query.set("purpose", options.purpose);
    }
    return this.request(
      `${this.prefix(tenantId, workspaceId)}/memories?${query}`,
    );
  }

  operationStatus(
    tenantId: UUID,
    workspaceId: UUID,
    operationId: UUID,
  ): Promise<OperationStatus> {
    return this.request(
      `${this.prefix(tenantId, workspaceId)}/operations/${operationId}`,
    );
  }
}
