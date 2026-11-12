# Isolate Invalid Song Identity From Global Metadata Backoff

## Problem

An existing recording has a title but no artist. The automatic web candidate
query selects it, and web payload validation raises `LlmConfigError` before any
HTTP request or budget reservation. The generic failure handler then pauses the
entire paid queue. Retrying the unchanged recording repeats the same failure.

## Intended Behavior

- Treat invalid per-recording web identity as a local, non-billable skip.
- Keep the existing recording metadata and attempt/accounting history intact.
- Continue processing unrelated candidates without a global pause.
- Do not weaken the requirements for title/artist, safe metadata input, official
  endpoint, supported model, request bounds, budget reservations, or evidence.
- Retain global fail-closed handling for actual configuration, transport, and
  accounting uncertainty. Do not catch every `LlmConfigError` as a local skip.
- Permit changed recording metadata to be reconsidered through the existing
  modification/version flow, without repeated attempts for unchanged bad input.

## Implementation

1. Distinguish per-recording input validation from API configuration failures.
2. Persist a terminal per-recording skip for invalid web identity without issuing
   HTTP requests or consuming budget; keep ordinary task/accounting semantics.
3. Add deterministic tests for missing/blank title and artist, other invalid
   identity input, continuation to valid candidates, idempotency, preserved
   metadata, and genuine configuration/budget failures.
4. Base the fix on deployed revision `0e41820` to retain recommendation changes.
5. Deploy a verified image with private backup and rollback; preserve active
   configuration, mounts, budget ledger, and unrelated runtime state.
6. Remove only the exact known obsolete pause after checking its provenance,
   budget safety, and corrected runtime; observe a scheduled background pass.

## Validation

- Run affected web/background/budget/routing tests and full Linux CI.
- Reproduce the live recording's local preflight with zero HTTP requests.
- Verify skipped recording state and successful unrelated background work after
  deployment. Keep USD 60 monthly, USD 3 safety buffer, and USD 2 daily limits.
- A processed or skipped web attempt is not evidence that missing musical facts
  were resolved. No client build or real-device playback acceptance is included.

## Progress

- [x] Read-only root cause confirmed; isolated worktree created from live source.
- [x] Dedicated recording-input error, atomic terminal skip, and 93 focused
  web/background/budget/pricing/routing tests passed. Fault injection confirmed
  skip persistence failures abort without paid continuation; the existing claim
  remains subject to stale-lock recovery rather than becoming a false terminal skip.
- [ ] CI/image verification.
- [ ] Reversible deployment and runtime queue verification.
