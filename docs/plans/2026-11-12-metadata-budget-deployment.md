# Metadata Budget Deployment Evidence

## Scope and Source

- User requested unattended metadata enrichment with a USD 60 monthly software limit.
- Target source: `a8b5fcfcdec3407470988f4d904c4c1f01aa024a`.
- Work used an isolated source worktree because another chat was changing recommendation behavior concurrently. Unrelated recommendation changes are excluded.
- Runtime was switched from `ebda453` to the target revision on 2026-11-12. Background enrichment and its shared spending controls are enabled.

## Observed Runtime Settings

| Setting | Target |
| --- | --- |
| Monthly allowance | USD 60 |
| Safety buffer | USD 3; effective reservation allowance USD 57 |
| Daily pacing | USD 2 |
| Calendar boundaries | Asia/Shanghai |
| Provider/model | Official OpenAI API / `gpt-6-luna` |
| Service tier / reasoning | `default` / `low` |
| Web tool-call limit | 3 per request |
| Web output limit | 4,096 tokens |
| Background interval / batch | 300 seconds / 3 candidates |
| Payload logging / path hints | Disabled / disabled |

These values were read back from the active container after cutover without printing credentials or private configuration.

## Confirmed Preflight

- Issued exactly 1 text and 2 web requests through the official API using the new-code overlay and existing MariaDB accounting store. No song metadata was written.
- All three charges were settled from returned usage. Conservative ledger total: **28,972 micro-USD (USD 0.028972)**; outstanding reservation: **0**.
- Text usage accounting passed. Web responses included `service_tier=default` and billable `tool_usage.web_search.num_requests`; tool action counts were not substituted for billable searches.
- One web response failed strict parsing; its exact rejection reason was not retained. Its charge was still accounted for.
- The diagnostic web retry parsed successfully for "时过境迁": identity matched, confidence `0.4`, language `null`. This is valid abstention, not successful language completion or proof of recommendation improvement.
- A subsequent parser fix accepts safe detail-page URLs returned by completed `open_page`/`find_in_page` API actions. Model-provided URLs must still match API provenance; unsafe or invented URLs remain rejected.

## Database and Automated Checks

- Actual MariaDB accounting check with four workers passed **8/8 operations**: reserve 1 micro-USD, settle at zero, repeat settlement. Final spending and reservations were unchanged.
- This followed a fix for duplicate period-row `INSERT IGNORE` lock contention.
- A 60,000,000 micro-USD reservation was denied with `month_limit`. These database checks issued no HTTP requests.
- First CI run executed 2,259 tests and exposed one facade import failure, subsequently fixed.
- CI for `af69462` passed full tests and packaging; its image build was canceled by the next push. It is not final-target image evidence.
- Local related suite passed 140 tests before the final rejection-isolation change. The subsequent budget/pricing/background/web suite passed 80 tests, including five new rejection-isolation tests. Counts overlap and must not be summed.
- Evidence rejection after completed usage settlement is terminal for that recording and ends the batch without globally pausing other songs. Unknown transport/accounting outcomes still retain reservations and pause the queue.

## Deployment and Rollback

- Prepared helper: `budget_cutover.py` in the local `supysonic-openai-20261112` lab directory.
- Staging creates private snapshots (directory `0700`, files `0600`) without changing the running service. Existing secret mount is reused; no new secret mount or key disclosure is required.
- Activation checks source revision, image revision, original container identity, and exact unchanged configuration hash before mutation. It rejects an existing rollback container name.
- Performed cutover: stopped the guard, retained/renamed the old container, replaced configuration, created/started the new container, passed the login-page health check, and restarted the guard.
- Rollback preserves the original container and exact configuration. Failure paths must not overwrite unrelated concurrent configuration/container changes.

## Runtime Verification

- [x] Final [CI run 43152966623](https://github.com/jsdfhasuh/Emosonic-Server/actions/runs/43152966623) succeeded for target `a8b5fcf`: 2,271 tests (3 skipped), network suite 5 tests (1 skipped), package checks, image revision check, and AMD64/ARM64 image publication.
- [x] Image: `ghcr.io/jsdfhasuh/emosonic-server@sha256:a7a3b34ac5e7206d76004726d73cce2c0231d15d22d898592db7bca0ef9d1a28`; revision label checked against the target before activation.
- [x] Container: `fd2aadcb10438f2070ab1acbde131929408ea02ce5bb50cf0deab5c52eaae904`, started `2026-11-12T04:26:23.707132925Z`, running, zero restarts at verification. Login-page health check passed; OpenWrt service guard reported `running`.
- [x] Active daemon and budget settings matched the table above. Preflight ledger survived container recreation.
- [x] First scheduled batch completed at 12:27:06 Asia/Shanghai: selected 3, text completions 2, web evidence rejection 1, skipped 0. Charges were 231, 220, and 14,638 micro-USD. Total including preflight: **44,061 micro-USD (USD 0.044061)**, outstanding reservations 0, accounting unblocked, no global paid-pause row.
- [x] The rejected web result was for "时过境迁" (`ebdd1c68-9f1e-4492-8032-6da533b717f5`). Its existing `zh` / `0.42` metadata was retained, with a terminal failed web marker. The task retry timestamp does not bypass that marker. This is rejection-path acceptance, not successful completion of that song's missing facts.
- [x] Rollback container retained: `supysonic-openwrt-before-budget-a8b5fcf`; private snapshots: `/mnt/django-emmc/supysonic/metadata-budget-a8b5fcf`.
- [x] Subsequent scheduled passes proceeded: bounded logs show five completed batches from 13:21 through 13:43 Asia/Shanghai, each selecting 3 and reporting 3 processed, 0 failed, and 0 skipped. These counters include inconclusive web results and do not imply all missing facts were resolved.
- [x] Read-only verification after the 13:48:46 batch showed total spending **253,685 micro-USD (USD 0.253685)**, outstanding reservations 0, accounting unblocked, and no global paid-pause row. A preceding in-flight snapshot showed a USD 1 web reservation, which was settled on completion.
- [x] The pinned rejected recording still had attempt count 2 and update timestamp 12:27:06, with its failed terminal marker and original `zh` / `0.42` unchanged. Later scheduled batches did not repeat that lookup. This continuation used read-only diagnostics and issued no manual paid requests.

## Limitations

This is an application-side conservative spending estimate, not an API-account invoice cap. The USD 1 web reservation is not a proven absolute single-request billing bound. Pricing changes, billing adjustments, taxes, and other applications or recommendation-chat calls sharing the key are outside this ledger.

Unknown request outcomes retain reservations. Uncertain accounting fails closed. A terminal web attempt is retained per recording to prevent repeated paid lookup; it does not automatically retry that recording indefinitely. Health/preflight checks do not establish long-term stability, better recommendations, or production readiness.
