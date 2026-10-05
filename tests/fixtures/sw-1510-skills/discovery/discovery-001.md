# Discovery — Assay envelope alignment (source-owned fixture, source 1)

## PRB-001: Methodology must never be silently substituted

The sequence surface carries methodology, autonomy_policy and session_schedule
as three separate fields. A legacy execution_mode or suggested_workflow value
that would route execution to a different methodology must be refused, not
coerced, so an Assay request can never fall through to Ralph.
