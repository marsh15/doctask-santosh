import { FormEvent, useEffect, useState } from "react";
import { loadWorkspace, submitDecisions } from "./api";
import type { Conflict, Proposal, Stage, WorkspaceData } from "./types";

type View = "timeline" | "evidence" | "decisions";
type Choice = { isApproved: boolean; reason: string; resolution?: Record<string, unknown> };

const viewLabels: Record<View, string> = {
  timeline: "Run timeline",
  evidence: "Evidence & conflicts",
  decisions: "Decision queue",
};

function Locator({ value }: { value: Record<string, unknown> }) {
  return <code className="locator">{Object.entries(value).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(" · ")}</code>;
}

function StatusMark({ status }: { status: string }) {
  return <span className={`status status--${status.toLowerCase()}`}>{status.replaceAll("_", " ")}</span>;
}

function Timeline({ stages }: { stages: Stage[] }) {
  if (stages.length === 0) return <EmptyState title="No stages recorded" body="This run has not produced an audit timeline yet." />;
  return (
    <ol className="provenance-rail" aria-label="Workflow stages">
      {stages.map((stage) => (
        <li key={stage.stage} className="stage-row">
          <span className="rail-node" aria-hidden="true" />
          <div className="stage-copy">
            <div className="row-heading"><h3>{stage.stage.replaceAll("_", " ")}</h3><StatusMark status={stage.status} /></div>
            <p>{stage.affectedEntityKeys.length} affected · {stage.skippedEntityKeys.length} preserved</p>
            <div className="entity-strip">
              {stage.affectedEntityKeys.map((key) => <code key={key} className="entity entity--affected">{key}</code>)}
              {stage.skippedEntityKeys.map((key) => <code key={key} className="entity entity--skipped">{key} · skipped</code>)}
            </div>
          </div>
          <time>{new Date(stage.startedAt).toLocaleString()}</time>
        </li>
      ))}
    </ol>
  );
}

function EvidencePanel({ proposals, conflicts }: { proposals: Proposal[]; conflicts: Conflict[] }) {
  if (proposals.length === 0 && conflicts.length === 0) {
    return <EmptyState title="No evidence under review" body="The run completed without proposed changes or competing claims." />;
  }
  return (
    <div className="evidence-layout">
      <section aria-labelledby="proposed-heading">
        <header className="section-heading"><div><p className="eyebrow">Provenance docket</p><h2 id="proposed-heading">Proposed mutations</h2></div><span>{proposals.length}</span></header>
        {proposals.map((item) => (
          <article className="ledger-entry" key={item.proposalId}>
            <div className="entry-title"><div><p className="eyebrow">{item.recordType}</p><h3>{item.recordId}</h3></div><code>{item.proposalId.slice(-8)}</code></div>
            <div className="diff-grid"><pre>{JSON.stringify(item.before, null, 2)}</pre><pre className="after">{JSON.stringify(item.after, null, 2)}</pre></div>
            {item.evidence.map((citation) => (
              <blockquote key={citation.spanId}>
                <p>“{citation.quote}”</p><Locator value={citation.locator} />
                <code className="hash">sha256 {citation.contentHash.slice(0, 16)}…</code>
              </blockquote>
            ))}
          </article>
        ))}
      </section>
      <section aria-labelledby="conflicts-heading">
        <header className="section-heading"><div><p className="eyebrow">Competing claims</p><h2 id="conflicts-heading">Conflicts</h2></div><span>{conflicts.length}</span></header>
        {conflicts.length === 0 ? <p className="quiet-panel">No supported claims conflict in this run.</p> : conflicts.map((conflict) => (
          <article className="conflict" key={conflict.conflictId}>
            <div className="row-heading"><h3>{conflict.recordId} · {conflict.field}</h3><StatusMark status={conflict.status} /></div>
            {conflict.claims.map((claim, index) => <pre key={index}>{JSON.stringify(claim.value, null, 2)}</pre>)}
          </article>
        ))}
      </section>
    </div>
  );
}

function DecisionQueue({ data, onCommitted }: { data: WorkspaceData; onCommitted: () => Promise<void> }) {
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const complete = data.run.status === "COMPLETED";

  function choose(proposalId: string, isApproved: boolean) {
    setChoices((current) => ({ ...current, [proposalId]: { isApproved, reason: current[proposalId]?.reason ?? "" } }));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const pending = data.run.reviewItems.filter((item) => item.reviewState === "PENDING");
    if (Object.keys(choices).length !== pending.length) return;
    setSubmitting(true); setError(null);
    try {
      await submitDecisions(data.run.runId, pending.map((item) => ({ reviewItemId: item.reviewItemId, ...choices[item.reviewItemId] })));
      await onCommitted();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Decision submission failed");
    } finally { setSubmitting(false); }
  }

  if (complete) return <EmptyState title="Review committed" body="This run is complete. The approved mutations are now part of the versioned control register." />;
  const pending = data.run.reviewItems.filter((item) => item.reviewState === "PENDING");
  return (
    <form onSubmit={submit} className="decision-form">
      {pending.map((item, index) => {
        const choice = choices[item.reviewItemId];
        const proposal = data.run.proposals.find((value) => value.proposalId === item.reviewItemId);
        const conflictClaims = item.kind === "CONFLICT" && Array.isArray(item.payload.claims)
          ? item.payload.claims as Array<{ value: Record<string, unknown> }>
          : [];
        return <fieldset key={item.reviewItemId} className="decision-card">
          <legend><span>{String(index + 1).padStart(2, "0")}</span>{item.subjectId}</legend>
          <p>{item.kind.replaceAll("_", " ").toLowerCase()} · review this item independently against its persisted payload.</p>
          {proposal?.evidence[0] ? <blockquote>“{proposal.evidence[0].quote}”</blockquote> : <pre>{JSON.stringify(item.payload, null, 2)}</pre>}
          {item.kind === "CONFLICT" ? <label>Supported resolution
            <select
              value={choice?.resolution ? JSON.stringify(choice.resolution) : ""}
              disabled={choice?.isApproved !== true}
              onChange={(event) => setChoices((current) => ({
                ...current,
                [item.reviewItemId]: {
                  ...current[item.reviewItemId],
                  resolution: JSON.parse(event.target.value) as Record<string, unknown>,
                },
              }))}
            >
              <option value="">Select one cited claim</option>
              {conflictClaims.map((claim, claimIndex) => <option key={claimIndex} value={JSON.stringify(claim.value)}>{JSON.stringify(claim.value)}</option>)}
            </select>
          </label> : null}
          <div className="choice-row">
            <button type="button" className={choice?.isApproved === true ? "selected approve" : ""} onClick={() => choose(item.reviewItemId, true)}>Approve</button>
            <button type="button" className={choice?.isApproved === false ? "selected reject" : ""} onClick={() => choose(item.reviewItemId, false)}>Reject</button>
          </div>
          <label>Review note <span>{choice ? "optional" : "choose approve or reject first"}</span><textarea disabled={!choice} value={choice?.reason ?? ""} maxLength={1000} onChange={(event) => setChoices((current) => ({ ...current, [item.reviewItemId]: { ...current[item.reviewItemId], reason: event.target.value } }))} /></label>
        </fieldset>;
      })}
      {error ? <p role="alert" className="error-banner">{error}</p> : null}
      <div className="submit-dock"><p>{Object.keys(choices).length} of {pending.length} decisions recorded</p><button className="primary" disabled={submitting || Object.keys(choices).length !== pending.length || pending.some((item) => item.kind === "CONFLICT" && choices[item.reviewItemId]?.isApproved && !choices[item.reviewItemId]?.resolution)}>{submitting ? "Committing…" : "Commit reviewed version"}</button></div>
    </form>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return <div className="empty-state" role="status"><span aria-hidden="true">§</span><h2>{title}</h2><p>{body}</p></div>;
}

export function App() {
  const [view, setView] = useState<View>("timeline");
  const [runId, setRunId] = useState(() => new URLSearchParams(location.search).get("run") ?? "");
  const [corpusId, setCorpusId] = useState(() => new URLSearchParams(location.search).get("corpus") ?? "");
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId || !corpusId) return;
    let cancelled = false;
    setLoading(true);
    loadWorkspace(runId, corpusId)
      .then((workspace) => { if (!cancelled) setData(workspace); })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Unable to load run");
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // The URL values are intentionally loaded once; later edits require explicit submission.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function refresh() {
    const workspace = await loadWorkspace(runId, corpusId);
    setData(workspace);
  }

  async function openDocket(event: FormEvent) {
    event.preventDefault(); setLoading(true); setError(null);
    try {
      await refresh();
      history.replaceState(null, "", `?run=${encodeURIComponent(runId)}&corpus=${encodeURIComponent(corpusId)}`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load run"); }
    finally { setLoading(false); }
  }

  return <div className="app-shell">
    <header className="masthead"><div><p className="eyebrow">Project delivery evidence system</p><h1>Control Register</h1></div>{data ? <div className="version-stamp"><span>REGISTER</span><strong>v{data.register.version}</strong></div> : null}</header>
    <form className="docket-bar" onSubmit={openDocket}>
      <label>Run ID<input value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="run:…" required /></label>
      <label>Corpus ID<input value={corpusId} onChange={(event) => setCorpusId(event.target.value)} placeholder="corpus:…" required /></label>
      <button className="primary" disabled={loading}>{loading ? "Opening…" : "Open review docket"}</button>
    </form>
    {error ? <p role="alert" className="error-banner">{error}</p> : null}
    {data ? <>
      <nav className="view-tabs" aria-label="Review views">{(Object.keys(viewLabels) as View[]).map((key) => <button key={key} aria-current={view === key ? "page" : undefined} onClick={() => setView(key)}>{viewLabels[key]}<span>{key === "timeline" ? data.stages.length : key === "evidence" ? data.run.proposals.length + data.conflicts.length : data.run.reviewItems.filter((item) => item.reviewState === "PENDING").length}</span></button>)}</nav>
      <main>
        <header className="page-heading"><div><p className="eyebrow">{data.run.runId}</p><h2>{viewLabels[view]}</h2></div><StatusMark status={data.run.status} /></header>
        {view === "timeline" ? <Timeline stages={data.stages} /> : view === "evidence" ? <EvidencePanel proposals={data.run.proposals} conflicts={data.conflicts} /> : <DecisionQueue data={data} onCommitted={refresh} />}
      </main>
    </> : <main><EmptyState title="Open a review docket" body="Enter a corpus and run identifier to inspect its persisted stages, grounded proposals, conflicts, and decisions." /></main>}
  </div>;
}
