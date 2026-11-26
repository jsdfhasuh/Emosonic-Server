# OpenAI music profiles and recommendations

The opt-in `recommendation_agent.provider = openai` path uses the official API.
Music profiles default to `gpt-6.2-sol`. Daily selection and chat use
`gpt-6-luna`. The analysis and song ordering use playback history, counts,
favorites and negative feedback from the database.

## Configuration

```ini
[recommendation_agent]
enabled = on
provider = openai
api_base_url = https://api.openai.com/v1
api_key =
api_key_file = /run/secrets/track_metadata_openai_key
model = gpt-6-luna
timeout_seconds = 120
reasoning_effort = low
profile_enabled = on
profile_model = gpt-6.2-sol
daily_selection_enabled = on
profile_refresh_interval_seconds = 21600
profile_batch_size = 2
cache_ttl_seconds = 900

[track_metadata_llm]
; Keep existing model/key/enrichment settings in this section.
monthly_budget_usd = 60
budget_safety_buffer_usd = 3
daily_budget_usd = 2
budget_timezone = Asia/Shanghai
```

Exactly one recommendation key source (`api_key`, `api_key_file`, `api_key_env`)
must be nonempty. Both services may reference the same private key file, but
credentials are never inherited from a different provider. Legacy compatible
provider mode is unchanged and is not covered by this OpenAI budget path.

## Background behavior

- A separate daemon profile job checks bounded users each tick. Successful
  profiles refresh no sooner than six hours; unchanged music facts are reused.
  A model change takes effect at the next eligible refresh. Failed attempts
  back off per user and retain the previous profile. Set
  `profile_model = gpt-6-luna` to use Luna for profiles.
- Daily playlists first recall at most 150 eligible local tracks. OpenAI ranks
  request-local IDs. Repeated IDs keep their first position, and omitted IDs
  caused by repeats are appended in local candidate order. Wrong lengths and
  unknown IDs are rejected.
  Listening/negative feedback eligibility and diversity are rechecked before save.
- Same-day existing playlists and API backfill remain non-billable. A deliberate
  first-day replacement needs a private playlist backup and a validated replacement;
  enabling the switch alone does not delete today's playlist.
- Chat still returns outside-library artists, retains filtering and cached
  follow-ups. The SSE endpoint emits the existing events after the completion has
  been accounted and validated; it does not expose unaccounted partial tokens.
- The server profile page/API only reads cached AI analysis. Flutter consumes
  unchanged daily song entries; no client build is needed for these features.

## Budget and failures

All three workloads reserve spending through the existing `mb:*` ledger before
HTTP and settle before using output. Metadata and recommendations together have
USD 57 admission capacity within the USD 60 monthly limit, and USD 2 daily pacing
in Asia/Shanghai. Existing counters are not reset; unknown-outcome reservations
are retained. Prices are conservative software estimates, not a provider invoice.

No hidden retries, repair requests, browsing tools or fallback providers are used
by the OpenAI recommendation path. A shared recommendation provider lease prevents
concurrent calls. Transient failures persist a 5/10/20/30-minute backoff for the
configured model. An accounted success resets that model's streak. The shared
ledger still limits admission across models. Authentication, quota and accounting
errors pause the affected model until the cause and persisted state are reviewed.

Logs use `openai_recommendation_accounted workload=profile|daily|chat` with
the model and micro-USD cost. Local deterministic tests mock
OpenAI; a live deployment still requires bounded profile/daily/chat probes and
separate user listening acceptance.

## Client acceptance

No Android or Windows application build is required. On each platform:

1. Open Discovery and pull to refresh daily recommendations. The existing daily
   playlist must open with real library songs and its normal cover/title fields.
2. Refresh again: the same day's order should remain stable, with no additional
   `recommendation_daily` reservation or accounted-call log.
3. Tap a recommended song once. Record expected versus actual title/audio and
   whether playback starts with one tap; backend health does not establish this.
4. Confirm favorites, play history and negative feedback retain their behavior.

For a playback mismatch, provide both Android controller and Windows authority
logs with the test time and expected/actual behavior. The default Windows log is
`%USERPROFILE%\Documents\debug.log`; it must not be edited or truncated. Android
logs may be collected read-only through an already-authorized ADB connection.
Server-side evidence is `openai_recommendation_accounted` and the persisted budget
workload records. Real-device and subjective listening acceptance are separate
from automated tests and the bounded live service probes.
