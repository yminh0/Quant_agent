# AI pipeline rebuild — local branch

## Decision

The public analysis-job API remains the compatibility boundary.  The execution
implementation behind it is replaced with an explicit, deterministic pipeline.
This branch is intentionally local-only (`codex/rebuild-ai-pipeline`); it must
not be pushed until an independent review and an isolated integration run are
complete.

## Why the old path is unfit for use

- `jobs.py` could replace a genuine provider error, deadline, capacity timeout,
  or clarification with a hard-coded high-performance report for one matching
  query.
- The replacement was enabled by default and made the browser unable to tell a
  real result from a fabricated one.
- The orchestration path depended on optional LangGraph availability.  The
  fallback and LangGraph paths encoded the same control flow twice.
- Generated candidate Python can reach an in-process `exec()` path.  It is not
  an acceptable production execution boundary.

## New execution contract

The pipeline has a single, inspectable order:

```text
Supervisor -> Ambiguity classifier -> Data -> Research
                                       |        |
                                      final    final
                                                |
Backtest code -> Backtest -> Signal -> Risk manager -> Report -> Envelope
```

Every transition receives the accumulated typed state and returns a state
patch.  A non-ready decision reaches `Envelope` immediately; it never becomes
a successful result.  The public API still owns queuing, capacity, cancellation,
deadline, durable state, and SSE events.

## Safety boundaries

- There is no query-triggered output replacement and no default synthetic
  performance data in the production package.
- A test needing a fake result must inject one as its runner; fakes cannot be
  activated by a user query or runtime environment switch.
- A real provider or data-source failure remains a typed failed job.  A
  clarification remains `need_clarification` and does not execute a backtest.
- Provider-supplied legacy Python is converted to typed structured parameters
  before normal execution.  Direct legacy candidates are never executed in the
  API or long-lived worker process: compatibility execution is a short-lived,
  time-bounded child process that returns JSON only.  A future production
  code-generation feature should replace that compatibility bridge with the
  backend subprocess executor and its stronger release/audit controls.

## Verification plan

1. Lock the job lifecycle with regression tests for provider error,
   clarification, and capacity timeout.  All must preserve the actual result.
2. Test the explicit pipeline's complete and three terminal short-circuit
   routes without LangGraph installed.
3. Run API/job contracts, lint, and the AI test suite in a repository-external
   virtual environment.
4. Do not claim a live provider, PostgreSQL, or capacity result from local
   fixtures.  Those need an isolated staging run with non-secret provenance.
