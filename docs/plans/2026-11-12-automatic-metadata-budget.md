# Automatic metadata enrichment with a software spending limit

## User decision

The user wants unattended background enrichment, including web lookup for
insufficient metadata, without a manual-song API or routine approvals. The user
requested a USD 60 monthly limit enforced by this software because the API
account cannot enforce it. This scope covers metadata calls only, not other
applications or recommendation-chat calls sharing the same API credential.

## Intended behavior

- Keep the current text enrichment path; automatically search only when metadata
  is low-quality or its language is unknown, including existing completed records.
- Use the official API, gpt-6-luna, low reasoning, bounded output, and no hidden
  HTTP retries for budgeted calls. Web lookup prioritizes exact song/album pages,
  matches artist/title/version, and keeps sourced facts distinct from estimates.
- Persist a shared calendar-month ledger using Asia/Shanghai month boundaries.
  Reserve a conservative amount atomically before every billable request, then
  settle from returned token/tool usage. Unknown outcomes retain the reservation.
- USD 60 is the configured monthly allowance; retain a USD 3 safety buffer.
  A USD 2 daily pacing limit avoids spending the entire allowance immediately.
  All text/search requests share these counters across workers and restarts.
- Stop when budget/configuration/accounting is uncertain; do not send a paid
  request on database errors, malformed usage, or an unknown model price.
- Keep source evidence bounded and sanitized in existing metadata JSON. Persist
  a web-attempt marker before network I/O to prevent crash/restart duplicate
  searches. A terminal inconclusive result stays unknown and does not loop.
- Automatically back off text failures; stop a failing batch promptly. Web
  attempts are terminal for the unchanged recording, including unknown outcomes.
  preserve good existing fields, and never alter the global recommendation gate
  or daily recommendation ranking in this task.
- Skip expensive full-library statistics in the background path. Keep existing
  CLI behavior and non-budgeted legacy installations compatible.

## Implementation and validation

1. Add a persistent atomic budget module and deterministic accounting tests.
2. Add a bounded, evidence-aware Responses web provider and transport tests.
3. Connect budget and automatic web fallback/backfill to daemon processing;
   cover selection, idempotency, stale locks, month rollover, and failures.
4. Document runtime defaults and limitations; run focused and CI regressions.
5. Deploy only after the safety checks pass. Verify the actual persistent budget
   settings and observe a bounded initial background batch before leaving it on.

## Limits

This is an application-side estimate/control, not an OpenAI account billing cap.
Pricing changes, taxes, third-party calls, and provider billing adjustments are
outside this ledger. Keep conservative reservations and a safety buffer, retain
uncertain charges, and do not claim exact invoice reconciliation.

## Progress

- [x] User chose automatic background processing and USD 60/month.
- [x] Persistent budget and provider implementation with focused tests.
- [x] Background integration and regression coverage: 159 focused tests run on
  Windows; 158 passed, with the previously reproduced baseline child-folder
  deletion test failing. All new budget/provider/background/routing tests pass.
- [ ] Deployment and bounded background verification.
