# PostgreSQL is the workflow system of record

Corpus evidence, workflow checkpoints, review decisions, dependency edges, and register
versions live in PostgreSQL rather than being split across transient queues or process memory.
This increases schema discipline and write coordination, but makes restart behavior and
optimistic concurrency independently inspectable and avoids two competing sources of truth.
