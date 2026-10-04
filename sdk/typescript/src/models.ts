export type UUID = string;
export type SemanticType =
  | "fact"
  | "preference"
  | "goal"
  | "constraint"
  | "episode";
export type Sensitivity = "normal" | "sensitive" | "restricted";
export type Lifetime = "session" | "temporary" | "durable";
export type OperationState = "pending" | "processing" | "completed";
export type EvidenceType =
  | "conversation_message"
  | "document"
  | "event"
  | "external_record";

export interface EvidenceReference {
  evidence_type: EvidenceType;
  reference_id: string;
  locator?: string | null;
}

export interface RememberMemoryRequest {
  subject_id: UUID;
  agent_id?: UUID | null;
  semantic_type: SemanticType;
  statement: string;
  sensitivity?: Sensitivity;
  lifetime?: Lifetime;
  purpose: string;
  access_scope?: string[];
  retention_until?: string | null;
  valid_from?: string | null;
  evidence?: EvidenceReference[];
}

export interface RememberMemoryResult {
  memory_id: UUID;
  version_id: UUID;
  operation_id: UUID;
  operation_status: OperationState;
  replayed: boolean;
}

export interface Memory {
  memory_id: UUID;
  version_id: UUID;
  version_number: number;
  tenant_id: UUID;
  workspace_id: UUID;
  subject_id: UUID;
  agent_id: UUID | null;
  semantic_type: SemanticType;
  lifecycle: "active" | "superseded" | "expired" | "revoked" | "deleted";
  statement: string;
  normalized_subject: string | null;
  normalized_predicate: string | null;
  normalized_value: unknown;
  qualifiers: Record<string, unknown>;
  valid_from: string;
  valid_to: string | null;
  recorded_at: string;
  sensitivity: Sensitivity;
  lifetime: Lifetime;
  origin: "explicit" | "extracted" | "derived";
  purpose: string;
  policy_version: string;
  access_scope: string[];
  retention_until: string | null;
  evidence: EvidenceReference[];
}

export interface MemoryList {
  items: Memory[];
}

export interface OperationStatus {
  operation_id: UUID;
  status: OperationState;
  attempts: number;
  last_error_code: string | null;
}
