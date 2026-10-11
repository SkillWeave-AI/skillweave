# Discovery — Assay envelope alignment (source-owned fixture, source 2)

## PRB-001: Methodology must never be silently substituted

The sequence surface carries methodology, autonomy_policy and session_schedule
as three separate fields. A legacy execution_mode or suggested_workflow value
that would route execution to a different methodology must be refused, not
coerced, so an Assay request can never fall through to Ralph.

## PRB-002: One fixture must traverse the whole chain

A single source-owned fixture traverses discovery, PRD, sequence, work
contract, dispatch and receipt with every constraint and exact-source binding
retained end to end.
