# Automatic track metadata enrichment

The daemon can enrich track recommendation metadata without a manual per-song
lookup endpoint. It first uses ordinary text enrichment, then performs a bounded
web lookup when the result is low-quality or the language is unknown. Existing
completed LLM records with those gaps are also eligible. This does not change the
recommendation quality gate or daily recommendation ranking.

## Configuration

The following example deliberately leaves the background worker disabled. Merge
these options into the existing sections; do not add duplicate section headers.
Choose exactly one key source, and never commit an API key or private config.

```ini
[daemon]
track_metadata_enrichment = off
track_metadata_enrichment_provider = llm
track_metadata_enrichment_interval = 300
track_metadata_enrichment_batch_size = 10
track_metadata_enrichment_stale_lock_seconds = 900
track_metadata_enrichment_send_path_hints = off
track_metadata_enrichment_log_payload = off

[track_metadata_llm]
api_base_url = https://api.openai.com/v1
model = gpt-6-luna
api_key_file = /run/secrets/track_metadata_openai_key
timeout_seconds = 60
max_output_tokens = 2048
token_limit_parameter = max_completion_tokens
temperature =
reasoning_effort = low
response_format = json_schema
monthly_budget_usd = 60
budget_safety_buffer_usd = 3
daily_budget_usd = 2
budget_timezone = Asia/Shanghai
web_search_enabled = on
web_max_tool_calls = 3
web_max_output_tokens = 4096
```

The dedicated section never inherits a key from `[recommendation_agent]`. An
absolute key-file path must exist inside the running container, preferably as a
read-only secret mount. `api_key_env` is an alternative to `api_key_file`, not an
additional source. The budgeted path currently accepts only the official OpenAI
endpoint and `gpt-6-luna`, whose pricing is explicitly handled by the code. Do not
change the model without updating and validating the pricing implementation.

After validating the configuration, secret mount, persistent database, and
running build, set `track_metadata_enrichment = on` and restart the daemon through
the deployment's normal service process. No client control or manual-song API is
needed. A sample configuration or passing unit test is not evidence that the live
daemon has been enabled or that real playback has been validated.

## Spending limits

- Text and web metadata calls share one database-backed ledger across daemon
  processes, metadata CLI calls, and restarts. Recommendation-chat calls and
  other applications using the same API key are outside this ledger.
- The monthly allowance is USD 60 with a USD 3 buffer: new requests must fit the
  effective USD 57 monthly admission limit and the USD 2 daily pacing limit.
- Periods follow the Asia/Shanghai calendar. Daily allowance resets at midnight;
  monthly allowance resets on the first day of the month. Unused allowance does
  not carry forward. Daily rollover does not clear a monthly exhaustion.
- Every request reserves a conservative amount atomically before network I/O.
  Text reservations depend on the bounded request and output allowance. Each
  web request currently reserves USD 1. Returned usage settles the reservation
  and releases any unused part; cache discounts are intentionally not credited.
- The full next reservation must fit. A worker can pause with a positive balance
  that is smaller than the next reservation. The daily limit is pacing, not a
  promise to spend exactly USD 2 or process a fixed number of songs each day.
- Lower limits can tighten an existing period. Raising configuration limits does
  not enlarge a cap already persisted for that period; the higher limit takes
  effect in a new period. Restarting does not reset spending or reservations.

These are application-side accounting and admission controls, not a guaranteed
OpenAI invoice ceiling. Reservations are conservative estimates, not proven
maximum external charges. Token usage and reported billable search requests are
counted, but API pricing changes, taxes, billing adjustments, and calls outside
this path are not covered. A web tool-action limit is not itself an exact count
of billable searches. If actual usage exceeds a reservation, the ledger records
it and blocks the affected periods; it cannot undo a charge already incurred.

Do not delete ledger records or restore an older database to recover allowance.
The `Meta` table contains `mb:m:*`, `mb:d:*`, and `mb:r:*` accounting records.
They are part of the persistent runtime state, not a disposable cache.

## Failure and retry behavior

A timeout, process interruption, missing usage, or uncertain billing outcome
keeps the outstanding reservation. It is never automatically refunded or expired.
Late settlement belongs to the original day and month, not the settlement date.
Database errors, corrupt accounting, or unsupported pricing fail closed before a
new paid request. Known daily/monthly exhaustion pauses until the relevant reset;
other accounting uncertainty pauses the paid worker for 24 hours.

A missing, blank, malformed, or unsafe song identity is a recording-local web
skip, not an API configuration failure. It records `web_lookup.status=skipped`,
`reason=invalid_identity`, and `request_sent=false`, preserves existing metadata,
and continues unrelated candidates without HTTP, budget reservation, or a global
pause. The unchanged recording is not retried after a restart. A recording
modification can make it eligible again through the normal refresh path. Genuine
endpoint/model/request-bound configuration errors still use global backoff.

Transient connection failures, timeouts, HTTP 408, ordinary rate limits (429),
and server errors (5xx) use a separate persistent provider backoff. Consecutive
failures wait 5, 10, 20, then at most 30 minutes, independent of any track's
lifetime attempt count. A numeric provider `Retry-After` may extend this delay,
up to 24 hours. An accounted successful response resets the observed failure
streak without clearing a newer concurrent failure. Authentication, quota,
configuration, unaccounted invalid responses, and unknown errors retain a
24-hour hard pause. Budget reset deadlines are not shortened by provider backoff.

An accounted but unusable text result is a track-local failure with a one-day
retry delay; other candidates continue. Rejected web evidence is also local.
The provider backoff lives in `Meta.metadata_provider_backoff`; the older
`metadata_paid_pause` remains a hard/budget pause. Do not automatically truncate
old pauses: their original failure classification may no longer be available.

Web attempts are different: a
marker is committed before the network call, and an attempted lookup is terminal
for that unchanged recording, including failed or inconclusive attempts. This
prevents repeated searches after restarts and means transient web failures are
not automatically retried. Changed track metadata/mtime can make a new recording
version eligible. No hidden HTTP retries or redirect following are used by the
budgeted transport.

## Evidence and metadata quality

Web lookup prefers exact song or album detail pages and matches title, artist,
album, year, and version where possible. A successful identity match alone is not
proof of language or mood. Language needs explicit source evidence; inferred
fields remain distinguished from sourced fields. No audio analysis is performed,
and web lookup does not invent energy, valence, or danceability values.

Sources must appear in the actual tool sources or citations. Bounded, sanitized
evidence and the attempt status are retained in metadata `raw_json.web_lookup`;
full pages and full lyrics are not stored. Existing high-quality fields are kept
when a candidate has no replacement value. Ambiguous results can remain unknown
or low-confidence, and a web lookup is not a factual-correctness guarantee.

## Operational checks

Use bounded daemon logs and look for these markers without dumping private
configuration, request bodies, or API keys:

- `metadata_budget_settled`: request kind and accounted `cost_micro_usd`.
- `metadata_budget_paused`: accounting prevented further paid work.
- `metadata_background_retry`: transient/hard class, safe HTTP status, consecutive
  transient failure count, transport category, and retry delay (no error body).
- `metadata_provider_paused`: daemon correctly reports provider backoff rather
  than claiming an empty batch completed successfully.
- `metadata_identity_skipped`: recording-local invalid identity, no paid request.

A `web_lookup` marker alone does not prove a search was sent: distinguish
non-billable `skipped` markers from completed, inconclusive, or failed requests
when reporting lookup counts and metadata coverage.

For read-only diagnostics, `MetadataBudget.snapshot()` exposes current day/month
spent, reserved, remaining, blocked status, and reset timestamps without keys or
request content. Amounts are integer micro-USD: 1,000,000 equals USD 1. Compare the
application ledger with the provider's usage report during rollout; do not clear
unknown reservations merely because no successful metadata result was written.
