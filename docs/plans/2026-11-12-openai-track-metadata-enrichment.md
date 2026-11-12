# OpenAI track metadata enrichment

## Problem

Track enrichment uses the recommendation chat agent's LLM configuration. The
current upstream returns 503 errors, leaving metadata missing and repeatedly
retrying older tracks. Daily recommendation ranking is out of scope here.

## Intended behavior

- Configure track enrichment independently for OpenAI or a compatible endpoint.
- Preserve existing installations through an explicit legacy configuration
  fallback when no dedicated track configuration is supplied.
- Support the token-limit and optional sampling parameters needed by the chosen
  model without changing the recommendation agent's existing request behavior.
- Keep API keys in private runtime configuration or a private key file, never in
  source control, logs, or diagnostic output.
- Validate a small number of tracks before enabling a backlog run. Do not
  overwrite existing metadata or reprocess the whole library by default.

## Implementation

1. Inspect provider, daemon, CLI, configuration, and existing tests.
2. Add dedicated track LLM configuration and one shared configuration resolver.
3. Extend the existing chat-completions transport only where required for model
   compatibility; retain current JSON validation and metadata quality gates.
4. Document configuration, bounded verification, and rollback.
5. After the user supplies endpoint/model/key location, validate connectivity,
   back up only the affected private runtime configuration, and run a small
   explicitly selected enrichment sample before considering background work.

## Tests

- Dedicated configuration does not alter the recommendation agent.
- Legacy fallback remains compatible; partial dedicated configuration fails
  clearly instead of mixing credentials across endpoints.
- Credentials and private-file errors are not exposed in logs.
- Request token limits and omitted temperature serialize correctly.
- Existing metadata provider, enrichment pipeline, daemon, and config tests pass.

## Assumptions and limits

- Runtime is the OpenWrt container `supysonic-openwrt`; do not use local Docker.
- The user selected the official OpenAI API and supplied a private local key
  file. Authentication and model-list access succeeded. Evaluate `gpt-6-luna`
  with no/low reasoning before writing sample metadata or enabling the backlog.
- Paid backlog volume is not yet specified. Do not infer credentials from another
  integration or run a full-library force refresh.
- Text-based enrichment estimates attributes from track identity and available
  tags; it does not listen to audio or guarantee factual language detection.
- Tracks without task rows can already be discovered by the normal collector.
  No library scan or bulk task reset is needed merely to configure OpenAI.

## Progress

- [x] Initial provider and queue investigation.
- [x] Implementation and focused automated tests (138/139 passed; one unchanged
  baseline directory-deletion failure).
- [x] User-supplied API authentication and ten bounded inference requests; no
  production database writes. Small-sample recommendation: `gpt-6-luna`, low
  reasoning, 1,536 token limit, strict JSON schema.
- [x] Deploy source and private runtime configuration, then verify selected DB writes.
  The user authorized a restart and exactly three selected songs. Runtime source
  is `ebda4536763e6f25c05d728e20d90cd628a32acf`; login, daemon, and guard checks
  passed. All three tasks completed and their metadata timestamps match the
  tracks. Only one sample passed the existing confidence/semantic quality gate.
- Deferred, not authorized: background activation or full-library enrichment.
  Automatic enrichment remains disabled; two low-confidence samples need review.
