# Metadata provider backoff correction

## Problem

The automatic metadata worker derives a global provider pause from a track's
lifetime attempt counter. Successful claims do not reset this counter, so an
isolated provider error on an old task can pause every track for 76,800 seconds.
The current error record also loses the safe HTTP classification needed to
distinguish transient failures from configuration and accounting failures.

## Intended behavior

- Keep the existing USD 60 monthly limit, USD 3 safety buffer, daily pacing,
  reserve-before-request accounting, and unknown-outcome reservations unchanged.
- Use durable provider-level consecutive transient failures, independent of
  track attempt history: 5, 10, 20, then at most 30 minutes. Reset after a
  successful accounted provider response. A provider Retry-After may extend the
  delay up to 24 hours; it cannot shorten the normal delay.
- Keep malformed song results local to the track; continue unrelated work.
- Keep authentication, quota, configuration, unknown errors, and unavailable
  accounting fail-closed. Do not silently clear budget pauses or reservations.
- Record only allowlisted error classification, HTTP status, and retry timing;
  never persist provider response text, credentials, or request contents.

## Implementation

1. Preserve safe failure details in budgeted text and web request adapters.
2. Add persistent transient backoff state with atomic updates and restart safety.
3. Separate per-track invalid results from service failures in the background loop.
4. Add focused tests, run related budget/web/routing suites, and review the diff.
5. Build and deploy only the revised server image, preserving runtime settings,
   mounts, unrelated recommendation changes, and a rollback container.
6. Back up and narrowly recover the verified legacy provider pause after cutover;
   retain all unresolved reservations. Observe natural worker progress.

## Tests and acceptance

- Large historical attempt counters do not influence transient delay.
- Consecutive failures increase delay, survive restart, cap at 30 minutes, and
  reset after a successful accounted response.
- Network timeouts, retryable HTTP statuses, hard failures, malformed text/web
  results, storage errors, and budget admission failures take distinct paths.
- No immediate HTTP retry, no duplicate web attempt, no secret/error-body logging.
- Unknown requests retain reservations; budget and hard-stop behavior remain safe.
- Live acceptance requires the new revision, healthy service, preserved config,
  and successful natural background progress; tests alone are not deployment.

## Assumptions and status

- Baseline: deployed code revision `3aa5b57`, with documentation commit `30ef17f`.
- No client changes or public release are needed.
- Implementation, focused tests, independent review, deployment, and bounded
  live verification completed on 2026-11-14.

## Verification result

- Code revision: `ae8d27d6942f48acc3a089d2f3b0109368b94f5c`.
- Focused offline suite: 118 tests passed. The first sandboxed combined run hit
  Windows temporary-directory permissions; the unchanged suite passed outside
  that sandbox, then passed again after final integration changes.
- Linux CI: 2,351 tests (3 skipped), plus 5 network tests (1 skipped), package
  checks, image revision validation, and AMD64/ARM64 publication succeeded.
- CI run: https://github.com/jsdfhasuh/Emosonic-Server/actions/runs/43543422280
- Image-only deployment passed health and exact configuration preservation
  checks, retaining the previous container for rollback.
- The precisely matched legacy pause was resumed once after backup and CAS
  checks. The recovery did not modify budget records or release reservations.
  The old record did not retain its HTTP status, so it was not retroactively
  classified as a proven transient error.
- The first natural batch at 10:14 Asia/Shanghai processed two text records and
  one web record: selected=3, enriched=3, failed=0. The original blocking task
  completed, both global pause states were absent, and new usage settled.
- An enriched web task can still have unknown language; task completion is not
  equivalent to fully resolved metadata. Long-term behavior remains subject to
  future provider failures and budget admission, not this bounded observation.
