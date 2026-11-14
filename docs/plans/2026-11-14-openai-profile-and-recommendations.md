# OpenAI listening profiles and recommendations

## Scope

The user confirmed migration of listening preference analysis, daily local-library
recommendations, and recommendation chat to OpenAI. Playback counts, timestamps,
favorites, and feedback remain database facts. All these calls share the existing
metadata allowance: USD 60 monthly, USD 3 buffer, USD 2 daily pacing. Existing
spending and unknown-outcome reservations must not be reset.

## Intended behavior

- Keep the existing tested `gpt-6-luna` model, standard processing, low reasoning,
  bounded input/output, and the existing private OpenAI key mount.
- Add one budgeted, tool-free JSON request path for profile, daily selection,
  and chat. Reserve before each HTTP request and settle before consuming output.
  No hidden retries, unmetered repair call, or fallback to the former provider.
- Maintain independent recommendation service backoff so a metadata-only pause
  cannot suppress a healthy recommendation request; share spending admission.
- Generate cached AI music-preference analysis from bounded aggregate facts.
  Do not infer health, religion, identity, or other sensitive traits from music.
  Refresh in the background; viewing statistics must not initiate a paid call.
- Retain deterministic local candidate recall, eligibility, feedback exclusion,
  and diversity checks. OpenAI selects/orders only validated candidate IDs; it
  cannot invent local songs or rewrite listening history.
- Preserve existing API and stream event shapes. If OpenAI is unavailable, retain
  prior valid profiles/playlists and return explicit chat unavailability instead
  of silently switching providers or replacing valid data with empty results.
- Expose cached AI analysis on the existing server Web UI. Flutter continues to
  consume existing recommendation song payloads; no client build is required.

## Implementation boundaries

1. Shared OpenAI recommendation transport, key loading, config, budget integration.
2. Cached background music profile and Web presentation/read-only API additions.
3. Bounded daily selection and validation, preserving same-day reuse and feedback.
4. Sync/stream recommendation chat migration with provider-aware cache identity.
5. Unit/integration tests, full Linux CI, small live probes and image/config cutover.

## Validation

- All paid paths reserve and settle; quota/budget/unknown usage fail closed.
- Stream and JSON responses preserve the existing public contract.
- Empty histories, incomplete metadata, multilingual preferences, duplicate or
  invented IDs, stale caches, negative feedback, and concurrent calls are covered.
- User data is bounded and minimized; no credentials, local paths, or raw logs
  enter prompts, responses, or diagnostics.
- Profile page reads remain non-billable; cached results and errors are explicit.
- Deployment retains rollback, unrelated settings, metadata backoff fixes,
  playback history, and budget records. Existing daily playlists are changed only
  after a validated OpenAI replacement is available and backed up.

## References and status

- https://developers.openai.com/api/docs/models/gpt-6-luna
- https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat
- https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra.md
- Baseline deployed code: `ae8d27d`; documentation baseline: `c62b224`.
- Implementation and acceptance: in progress.
