export type Evidence = {
  spanId: string;
  sourceId: string;
  sourceVersionId: string;
  quote: string;
  locator: Record<string, unknown>;
  contentHash: string;
};

export type Proposal = {
  proposalId: string;
  recordId: string;
  recordType: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown>;
  evidence: Evidence[];
};

export type Run = {
  runId: string;
  corpusId: string;
  baseRegisterVersion: number;
  status: "RUNNING" | "AWAITING_REVIEW" | "COMPLETED";
  proposals: Proposal[];
  reviewItems: ReviewItem[];
};

export type ReviewItem = {
  reviewItemId: string;
  kind: "PROPOSED_MUTATION" | "CONFLICT" | "RULE_FINDING";
  subjectId: string;
  payload: Record<string, unknown>;
  reviewState: "PENDING" | "APPROVED" | "REJECTED";
};

export type Stage = {
  stage: string;
  status: string;
  affectedEntityKeys: string[];
  skippedEntityKeys: string[];
  startedAt: string;
  completedAt: string | null;
};

export type RegisterRecord = {
  recordId: string;
  recordType: string;
  value: Record<string, unknown>;
  evidence: Evidence[];
  canonicalHash: string;
};

export type Register = {
  corpusId: string;
  version: number;
  records: RegisterRecord[];
};

export type Conflict = {
  conflictId: string;
  recordId: string;
  field: string;
  claims: Array<{ value: unknown; evidence: Evidence[] }>;
  status: string;
};

export type WorkspaceData = {
  run: Run;
  stages: Stage[];
  register: Register;
  conflicts: Conflict[];
};
