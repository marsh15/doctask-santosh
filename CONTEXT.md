# Project Delivery Control

This context governs how changing project evidence becomes a reviewed, versioned control
register without hiding uncertainty or contradicting sources.

## Language

**Corpus**:
A bounded collection of related project sources evaluated together.
_Avoid_: Workspace, knowledge base

**Source**:
The stable identity of an input document within a Corpus.
_Avoid_: File, attachment

**Source Version**:
One immutable byte-level revision of a Source.
_Avoid_: Upload, copy

**Source Span**:
A precisely locatable excerpt from one Source Version that can support a claim.
_Avoid_: Chunk, passage

**Claim**:
A structured factual assertion extracted from one or more Source Spans.
_Avoid_: Answer, fact

**Evidence Reference**:
A citation from a Claim or Register Record to the exact Source Span that supports it.
_Avoid_: Link, source note

**Control Register**:
The current human-approved set of project milestones, risks, actions, decisions,
dependencies, scope changes, conflicts, and evidence gaps.
_Avoid_: Report, dashboard

**Register Record**:
One stable, typed entry in the Control Register.
_Avoid_: Finding, row

**Conflict**:
Two or more supported Claims that disagree about the same field and remain unresolved.
_Avoid_: Error, latest value

**Evidence Gap**:
An explicitly unknown value whose available Source Spans do not provide adequate support.
_Avoid_: Guess, missing data

**Proposed Mutation**:
A reviewable candidate change to a Register Record that has not become authoritative.
_Avoid_: Update, edit

**Human Decision**:
An approve or reject choice for exactly one Proposed Mutation or conflict resolution.
_Avoid_: Approval batch, feedback

**Workflow Run**:
One resumable analysis of one Corpus against a specific base Register Version.
_Avoid_: Job, request

**Register Version**:
An immutable, numbered snapshot produced from approved Proposed Mutations.
_Avoid_: State, revision
