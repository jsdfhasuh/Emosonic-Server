# OpenAI track metadata enrichment

Track enrichment uses the existing OpenAI-compatible Chat Completions client.
It sends track identity and tags, not audio. Language, mood, scene, and energy
are model estimates; structured output validates shape, not factual accuracy.
Daily recommendation ranking and the recommendation chat agent are unchanged.

## Independent configuration

Add the following to the server's private INI configuration, selecting a model
available to the API project. Do not commit real credentials.

```ini
[track_metadata_llm]
api_base_url = https://api.openai.com/v1
model = gpt-6-luna
api_key_file = /run/secrets/track_metadata_openai_key
timeout_seconds = 60
max_output_tokens = 1536
token_limit_parameter = max_completion_tokens
temperature =
reasoning_effort = low
response_format = json_schema

[daemon]
track_metadata_enrichment = no
track_metadata_enrichment_provider = llm
track_metadata_enrichment_batch_size = 3
track_metadata_enrichment_interval = 300
track_metadata_enrichment_send_path_hints = no
track_metadata_enrichment_log_payload = no
```

The key file must contain only the API key and an optional trailing newline.
Use an absolute path visible inside the container, a read-only mount, and
restrict file permissions to the service user. Alternatively, set
`api_key_env = OPENAI_API_KEY` and provide that variable to the container.
Exactly one of `api_key`, `api_key_file`, or `api_key_env` must be configured.
No credentials are inherited from another provider once dedicated settings exist.
URLs with embedded credentials, query parameters, or fragments are rejected.

With no dedicated settings, legacy `recommendation_agent` settings remain in use.
The new configuration does not modify or enable that chat agent. The metadata
provider/source values remain `llm`, preserving downstream quality checks.

`token_limit_parameter` accepts `max_tokens` (legacy default) or
`max_completion_tokens`. A blank `temperature` omits the sampling parameter;
when absent, the metadata provider keeps its existing 0.2 default. Select values
supported by the actual model. The output-token limit includes reasoning tokens
for applicable models, so a small limit can truncate the metadata response.
`reasoning_effort = none` avoids reasoning-token spending on supported models.
The initial small music sample favored `low`: it corrected the language of a
known Korean song supplied with only its English title. This is a limited sample,
not a benchmark or a guarantee for unfamiliar songs.
Leave it blank to use the model default; invalid effort values are rejected.

`response_format = json_schema` requests strict structured output and does not
silently retry without the schema. Use `json_object` for compatible gateways
without structured-output support. Refused and truncated responses are rejected.
Official references:

- [Chat Completions parameters](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)
- [GPT-6 Luna model and pricing](https://developers.openai.com/api/docs/models/gpt-6-luna)

## Bounded verification

1. Verify the running container, its configuration/key mounts, and the selected
   source revision. Do not assume the local Docker daemon is the OpenWrt host.
2. Back up the private runtime configuration with restricted permissions. Keep
   background enrichment disabled while testing; changing the INI alone does not
   reload an already running daemon. Arrange a controlled daemon reload before
   treating background processing as disabled or enabled.
3. Run one missing track through the CLI inside the deployed container:

   ```sh
   supysonic-cli metadata enrich --provider llm --track-id TRACK_UUID --limit 1
   ```

   This writes metadata and calls the paid API. The CLI initializes the database
   and can apply pending schema migrations, so use the matching deployed revision.
   Always specify `--provider llm`; the default local provider is not equivalent.
4. Expect `Selected: 1, enriched: 1, failed: 0` and inspect the selected track's
   `track_metadata` and task row: provider/source `llm`, chosen model, nonempty
   semantic fields, plausible language/confidence, and task status `completed`.
   Do not infer semantic correctness from a successful HTTP response alone.
5. Repeat for two or three known songs with different languages. Check task
   `last_error` and bounded daemon logs on failure, without dumping configuration,
   credentials, full request payloads, or HTTP authorization headers.
6. Only after sample acceptance and an agreed backlog/budget, enable background
   enrichment with a small batch and verify its completed/failed counters.
   `track_metadata_enrichment_started` and
   `track_metadata_enrichment_provider_unavailable` identify daemon startup and
   configuration failures; `track_metadata_enrichment_provider_retry` identifies
   provider failures.

Missing tracks without task rows are discovered automatically. Existing current
metadata is skipped. Old `failed` rows are not automatically retried; use
`--failed-only` and explicit `--track-id` selections, or a deliberately selected
`--force`, after fixing their cause. Do not force the entire library.

The CLI's `--dry-run` does not call the provider or write metadata, but it can
recover stale running-task locks. It is not a strict read-only database probe.

## Rollback and limitations

Disable background enrichment and reload only the affected service before
restoring the private config. Removing the dedicated section resumes legacy
configuration; keep enrichment disabled if the legacy upstream is still failing.
Changing provider settings does not erase or regenerate successful metadata.

This change does not redesign queue fairness/retry backoff, normalize historical
language labels, or add language weighting to daily recommendations. A healthy
provider can drain eligible retries; permanently failed items need bounded review.

## Verification on 2026-11-12

- Official API authentication and model-list access succeeded using a
  user-supplied private key file. No key contents were logged or committed.
- Ten bounded inference requests succeeded without database writes: eight with
  reasoning disabled, two with low reasoning. Total API-reported usage was
  4,466 input and 1,434 output tokens, approximately USD 0.001164 at the observed
  standard rates of USD 0.10/0.50 per million input/output tokens.
- Common Mandarin, Cantonese, English, and Korean examples returned plausible
  language labels when sufficient track identity was supplied. An English-only
  title was misclassified with reasoning disabled and corrected with low
  reasoning. Unfamiliar tracks remained low-confidence or unknown.
- OpenWrt can reach the official API (an unauthenticated probe returned 401).
- Focused regression suite: 139 tests, 138 passed. The directory-deletion
  cleanup test failed identically in a separately extracted HEAD baseline; no
  deletion logic was changed.
- Linux Python 3.12 CI passed: 2,186 unit tests (3 skipped), then 5 network-suite
  tests (1 skipped). Package builds and both Docker architectures succeeded in
  [Docker Publish run 43138513230](https://github.com/jsdfhasuh/Emosonic-Server/actions/runs/43138513230).

## Authorized OpenWrt cutover

The user authorized a service restart and exactly three specified missing tracks.
The new container started at 2026-11-12 10:16:26 Asia/Shanghai, using source
`ebda4536763e6f25c05d728e20d90cd628a32acf` and image
`ghcr.io/jsdfhasuh/emosonic-server@sha256:9d4280d44c491518352fd86121e28b76734cb2b4a6fe694cb6e4279f76e8e8ff`.

Verified after the cutover:

- Login HTTP 200, daemon process, and OpenWrt guard running; container restart
  count remained zero during verification.
- Dedicated configuration resolves to the official API, `gpt-6-luna`, and low
  reasoning. Background enrichment, path hints, and payload logging are off.
- Key mounted read-only from a mode-0600 file within a mode-0700 private directory.
  The recommendation agent's configuration is unchanged.
- Bounded CLI result: `Selected: 3, enriched: 3, failed: 0, skipped: 0`.
  Read-only database verification found exactly these three rows for the new model,
  with provider/source `llm`, task status `completed`, and matching track mtimes.

| Track ID | Model language | Confidence | Recommendation quality gate |
| --- | --- | --- | --- |
| `36d88242-7839-4f7f-9532-3898461ee2ca` | `zh` | 0.87 | Passed |
| `ebdd1c68-9f1e-4492-8032-6da533b717f5` | `zh` | 0.42 | Excluded |
| `5bfb5273-6b98-46e2-a3eb-d0cb96033ab1` | `zh` | 0.30 | Excluded |

The gate requires confidence >= 0.5 plus semantic fields and an LLM source.
The last two records are stored but do not contribute to recommendation semantic
profiles or candidate semantic scores. Completed tasks and model-generated
language labels are not independent factual verification. No audio was analyzed.
No additional paid enrichment or full-library activation was performed.

Rollback assets remain on OpenWrt:

- Previous stopped container: `supysonic-openwrt-before-openai-ebda453`.
- Private configuration and container backup directory:
  `/mnt/django-emmc/supysonic/openai-metadata-ebda453`.

A rollback must coordinate the OpenWrt guard, restore the previous configuration
and container name, then verify login and daemon health. The original config had
background enrichment enabled against the failing legacy provider; keep it disabled
when preparing a rollback unless explicitly asked to resume those retries.
Do not copy private backup contents into the repository. Successful sample rows
are not automatically removed by a container rollback.

Client playback/reconnect and long-term operation were not tested. This cutover
does not fix daily recommendation language weighting or establish that the two
low-confidence songs were accurately characterized. Background enrichment remains
disabled pending a separate volume/budget decision and sample-quality review.
