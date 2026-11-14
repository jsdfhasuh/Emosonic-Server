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
- Implementation and verification: in progress.
