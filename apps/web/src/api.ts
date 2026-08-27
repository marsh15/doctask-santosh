import type { WorkspaceData } from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = body?.detail?.message ?? `Request failed with status ${response.status}`;
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export async function loadWorkspace(runId: string, corpusId: string): Promise<WorkspaceData> {
  const [run, timeline, register, conflicts] = await Promise.all([
    request<WorkspaceData["run"]>(`/v1/runs/${encodeURIComponent(runId)}`),
    request<{ stages: WorkspaceData["stages"] }>(
      `/v1/runs/${encodeURIComponent(runId)}/timeline`,
    ),
    request<WorkspaceData["register"]>(
      `/v1/corpora/${encodeURIComponent(corpusId)}/register`,
    ),
    request<{ conflicts: WorkspaceData["conflicts"] }>(
      `/v1/corpora/${encodeURIComponent(corpusId)}/conflicts`,
    ),
  ]);
  if (run.corpusId !== corpusId) {
    throw new Error("The run does not belong to the requested corpus");
  }
  return { run, stages: timeline.stages, register, conflicts: conflicts.conflicts };
}

export async function submitDecisions(
  runId: string,
  decisions: Array<{
    reviewItemId: string;
    isApproved: boolean;
    reason?: string;
    resolution?: Record<string, unknown>;
  }>,
): Promise<void> {
  await request(`/v1/runs/${encodeURIComponent(runId)}/decisions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions }),
  });
}
