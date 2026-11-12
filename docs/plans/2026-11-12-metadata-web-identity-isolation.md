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
- [x] Full Linux CI succeeded: 2,326 main tests (3 skipped), 5 network tests
  (1 skipped), package checks, image revision check, and AMD64/ARM64 publication.
- [x] Image-only deployment, exact runtime preservation, guarded obsolete-pause
  recovery, and a natural scheduled background pass verified on OpenWrt.

## Deployment Evidence

- Fix source: `3aa5b57d933de7fefc2a0e667d537bf7ffb07911`; ancestry from
  deployed `0e41820a72ee630e3e38454485d1200c86f77b63` was verified.
- [CI run 43207863540](https://github.com/jsdfhasuh/Emosonic-Server/actions/runs/43207863540)
  succeeded for the exact fix source, including multi-platform image publishing.
- Image-only deployment helper snapshots the current container definition and
  exact private configuration, checks stable runtime settings before replacement,
  retains rollback, and never rewrites configuration on the success path.
- Queue recovery verifies the corrected source, zero outstanding reservations,
  the target recording's local identity error, and an exact task/pause deadline
  match before backing up and clearing only that obsolete pause with CAS guards.
- Deployed immutable image
  `ghcr.io/jsdfhasuh/emosonic-server@sha256:15f829ae97a2cdcbdf68a7d12732252832d8489bbc923c10e5b1f7ec8fb7f38e`.
  Container `fe9f84c69a530b9adeeba26f402c30775a31b9a1716926ed4fba25f7b815238f`
  started at `2026-11-12T11:30:32.644971706Z`, passed its login-page health
  check, and was running with zero restarts at verification. Exact configuration,
  mounts, host settings, and stable network settings matched preserved state.
- Rollback container: `supysonic-openwrt-before-identity-3aa5b57`; private
  deployment snapshots: `/mnt/django-emmc/supysonic/metadata-identity-3aa5b57`.
- Queue recovery matched legacy task attempt 4 and pause epoch `1794486933`,
  privately backed up both, cleared only that pause, and made only that task due.
  It issued zero HTTP requests. Spending stayed at USD `0.926985`, reservations
  stayed zero, and the USD 60/month, USD 3 buffer, and USD 2/day limits survived.
- The initial scheduler pass occurred before recovery and correctly honored the
  old pause. The next natural pass completed at 19:36:08 Asia/Shanghai: selected
  3, enriched 2, failed 0, skipped 1. The two ordinary requests settled at 212
  and 235 micro-USD. The known identity-invalid recording became a terminal
  `invalid_web_identity` task with `web_lookup.status=skipped` and
  `request_sent=false`; its existing unknown language/confidence were retained.
- After that pass, total spending was USD `0.927432`, reservations were zero,
  accounting was unblocked, and no global pause existed. The older rejected
  recording `ebdd1c68-9f1e-4492-8032-6da533b717f5` still had attempt count 2
  and its original 12:27:06 update,
  confirming it was not retried by recovery. No manually triggered paid lookup
  was used. A skipped recording is not successfully completed musical metadata.
