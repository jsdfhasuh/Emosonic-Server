# EmoSonic Web strict-v2 verification record

> This record captures the browser baseline before the 2026-07-14 code-review
> fixes. The current automated regression results and the still-pending
> post-fix browser rerun are documented in
> `docs/verification/emosonic_web_strict_v2_post_review_20260714.md`. Asset
> hashes and browser results below must not be treated as evidence for the
> post-review source tree.

Date: 2026-07-13 (Asia/Shanghai)

Status: The Web strict-v2 implementation and its Goal-level verification are
complete. Production rollout, formal conformance readiness, and replacement of
the user-owned `local-test-only:` manifest evidence remain independent review
decisions.

## Frozen inputs

- Repository base commit: `7f0015b2c5fb4657e4e2f120aa674e55e8164d01`
- Wire contract: `specs/emosonic_strict_v2_socketio_server_contract.md`
- Contract SHA-256:
  `ca069c6ad52447ea4f7ace7d795460c5ec759e5708b2f45acfbe50903aa4b3a3`
- Python: `3.13.11`
- Node.js: `v18.20.8`
- Sphinx: `9.1.0`
- Playwright: `1.55`
- Chromium: `140.0.7339.16`
- Firefox: `141.0`

The source tree was intentionally dirty during verification. Existing
`local-test-only:` conformance changes and `test_logs/` were preserved as
user-owned state.

## Capability and rollout boundary

Production configuration defaults remain fail-closed:

- `emo_web_realtime_protocol = legacy`;
- Web Follow, Handoff, and Broadcast gates default to `off`;
- Web gates do not override server deployment or conformance readiness.

The isolated browser acceptance application explicitly selected `strict_v2`,
enabled all three Web optional-profile gates, and negotiated the capabilities
only after the corresponding implementation was present:

| Client | Core | Broadcast | Follow | Handoff prepare/effective-at |
| --- | --- | --- | --- | --- |
| Chromium Web Player | enabled | enabled | enabled | enabled / enabled |
| Firefox Web Player | enabled | enabled | enabled | enabled / enabled |
| Mobile Web Control Console | enabled | enabled | controller does not follow | controller does not prepare |

The acceptance fixture raised only its local Handoff/request rate limits, with
`local-test-only: 30-sample strict web Handoff acceptance` evidence, so the
required 30 samples could run without changing production defaults.

## Runtime and acceptance asset hashes

| Asset | SHA-256 |
| --- | --- |
| `supysonic/emo/browser_auth.py` | `a39e95eb39fcb0e199bb0d348448b8cbf237c4df8222cb168b7c1a713a5b57e4` |
| `supysonic/static/js/emo_strict_v2_client.js` | `2210c7ec0e8a7e5e71e5a9b0144ada89e744da560bd51949813c259abd404fdf` |
| `supysonic/templates/player_strict_v2.html` | `33ee4f84ab47260d05b78852fb2bb5e9cff624dd181b327ccfde09c0e345b88c` |
| `supysonic/templates/control_strict_v2.html` | `6adeea9a226139076e51e97d4c0ee8e7edfdb8f9e70f37e6fac2a635632c5b72` |
| `script/serve_emo_web_strict_v2_acceptance.py` | `a6d51cb49e44ca7344bf2aa9f8e7f09d6bf5edffebc4aa9697295821b33c1978` |
| `tests/browser/emo_web_strict_v2_acceptance.js` | `b6ea2a71af3a175dbbf04f48c17e4e4b7b91b6e08b13104706a753ed2ca3821c` |

## Real-browser acceptance

Command:

```bash
NODE_PATH=/tmp/emosonic-playwright/node_modules \
PLAYWRIGHT_BROWSERS_PATH=/tmp/emosonic-playwright-browsers \
node tests/browser/emo_web_strict_v2_acceptance.js
```

Result: PASS.

Matrix and runtime evidence:

- Chromium desktop Web Player: `140.0.7339.16`;
- Firefox desktop Web Player: `141.0`;
- Chromium mobile Web Control: viewport `390 x 844`, document width `390`,
  no overflow offenders;
- distinct player identities:
  `web-player-bfbd2774-8588-42c3-aa2c-8225650596e5` and
  `web-player-a7d463cf-59d3-4e01-a731-a5f9695d7082`;
- control identity:
  `web-control-11311d0d-154c-415a-8849-e4b12745dc91`;
- no uncaught page errors.

Completed browser scenarios:

1. Chromium/Firefox/mobile strict bootstrap;
2. Context creation and duplicate-queue rejection;
3. same-browser secondary-tab owner lock;
4. refresh identity and Context recovery;
5. no-user-gesture Handoff rejection;
6. real local audio playback in both players;
7. remote control and stale-cursor refresh/retry;
8. real `capability_required` error UI;
9. player-owned Follow continuity without periodic backward seeks, followed by
   source network-loss cleanup;
10. real server restart and automatic strict reconnect;
11. two-browser Broadcast start/control/queue-sync/stop;
12. Context close and new tombstone-safe Context ID;
13. hidden-page Handoff rejection before commit;
14. 30 foreground Handoff timing samples;
15. protocol-error UI with no legacy fallback.

### Handoff timing

- Sample count: `30`
- Maximum absolute error: `26.776123046875 ms`
- Mean absolute error: `3.29 ms`
- p95 absolute error: `17.608154296875 ms`
- Samples at or below `200 ms`: `30 / 30`

The first 15 rows targeted the Chromium player and the remaining 15 targeted
the Firefox player. Times are calibrated server milliseconds.

| # | Target | Handoff ID | effectiveAtServerMs | actualStartServerMs | Absolute error ms |
| ---: | --- | --- | ---: | ---: | ---: |
| 1 | Chromium | `handoff-8d22900e1944` | 1783959027221 | 1783959027222.0845 | 1.08447265625 |
| 2 | Chromium | `handoff-da0f62bbe7a5` | 1783959029808 | 1783959029809.3638 | 1.36376953125 |
| 3 | Chromium | `handoff-667ec7f77a1d` | 1783959033068 | 1783959033069.1292 | 1.129150390625 |
| 4 | Chromium | `handoff-3e702eb4ea9a` | 1783959034556 | 1783959034557.4534 | 1.453369140625 |
| 5 | Chromium | `handoff-1611ed469a21` | 1783959035875 | 1783959035875.8147 | 0.814697265625 |
| 6 | Chromium | `handoff-338293ce6045` | 1783959037103 | 1783959037103.7607 | 0.7607421875 |
| 7 | Chromium | `handoff-a76f5ef0dff3` | 1783959038814 | 1783959038840.7761 | 26.776123046875 |
| 8 | Chromium | `handoff-19938b841845` | 1783959040208 | 1783959040209.4548 | 1.454833984375 |
| 9 | Chromium | `handoff-e50d82fdb395` | 1783959041567 | 1783959041567.8828 | 0.8828125 |
| 10 | Chromium | `handoff-9a7fce7be958` | 1783959042865 | 1783959042865.9604 | 0.96044921875 |
| 11 | Chromium | `handoff-b014113c2265` | 1783959044090 | 1783959044092.1023 | 2.102294921875 |
| 12 | Chromium | `handoff-d778083ca240` | 1783959045382 | 1783959045383.3176 | 1.317626953125 |
| 13 | Chromium | `handoff-2fd767fc2e96` | 1783959046682 | 1783959046683.5632 | 1.563232421875 |
| 14 | Chromium | `handoff-f8c16f9acaa3` | 1783959047934 | 1783959047935.0815 | 1.08154296875 |
| 15 | Chromium | `handoff-c44f74d879d5` | 1783959049281 | 1783959049282.4067 | 1.40673828125 |
| 16 | Firefox | `handoff-b7556386b772` | 1783959024319 | 1783959024319.5906 | 0.590576171875 |
| 17 | Firefox | `handoff-c7e047cdea13` | 1783959028419 | 1783959028419.6443 | 0.644287109375 |
| 18 | Firefox | `handoff-6cab99910277` | 1783959031806 | 1783959031806.2612 | 0.26123046875 |
| 19 | Firefox | `handoff-05447ce44c6a` | 1783959033936 | 1783959033953.6082 | 17.608154296875 |
| 20 | Firefox | `handoff-640fd1ed1951` | 1783959035257 | 1783959035258.7039 | 1.703857421875 |
| 21 | Firefox | `handoff-1cef3c55dff8` | 1783959036466 | 1783959036467.9902 | 1.990234375 |
| 22 | Firefox | `handoff-17577d690939` | 1783959037951 | 1783959037968.3428 | 17.3427734375 |
| 23 | Firefox | `handoff-bf4c4e3cff9b` | 1783959039547 | 1783959039548.685 | 1.68505859375 |
| 24 | Firefox | `handoff-a7f90918f5c1` | 1783959040936 | 1783959040937.5972 | 1.59716796875 |
| 25 | Firefox | `handoff-1e620f020a58` | 1783959042217 | 1783959042218.681 | 1.680908203125 |
| 26 | Firefox | `handoff-e02d05077c80` | 1783959043518 | 1783959043517.2097 | 0.790283203125 |
| 27 | Firefox | `handoff-92ef9ea850e0` | 1783959044753 | 1783959044754.4849 | 1.48486328125 |
| 28 | Firefox | `handoff-d3f70f9d7e87` | 1783959046085 | 1783959046088.7532 | 3.753173828125 |
| 29 | Firefox | `handoff-dad2dd9ffbcf` | 1783959047332 | 1783959047333.4312 | 1.43115234375 |
| 30 | Firefox | `handoff-f854100795cb` | 1783959048652 | 1783959048653.9724 | 1.972412109375 |

## Acceptance issues resolved with regression coverage

- SQLite strict Context mutations now acquire an immediate write transaction
  before their read phase, preventing deterministic cross-Context writer-upgrade
  failures in the threaded application. The focused concurrent regression test
  passes in `tests.base.test_emo_ws_store`.
- A Handoff `player.play` commit is accepted by the prepared target even though
  it does not yet own the source Context; the accepted prepare still validates
  the commit identity and cursor.
- Source release clears local authority ownership immediately while preserving
  best-effort final paused feedback for the released Context.
- Player feedback is serialized, so concurrent media events cannot race
  `clientSeq`; disconnect-time feedback is best-effort and does not create an
  uncaught page error.
- The acceptance harness attributes page errors, suppresses noisy access logs,
  waits through restart transitions, and uses explicit local acceptance quotas.

## Automated verification

### Web, config, fixture, and store tests

```bash
python -m unittest \
  tests.frontend.test_player \
  tests.frontend.test_device_alias_display \
  tests.frontend.test_web_strict_v2 \
  tests.base.test_config \
  tests.base.test_emo_web_strict_v2_fixtures \
  tests.base.test_emo_web_strict_v2 \
  tests.base.test_emo_ws_store
```

Result: PASS, 69 tests.

This covers browser OTP issuance and replay/session binding, strict rendered
pages, exact registration and profile gates, legacy rendering, Context binding
isolation, Core/Broadcast/Follow/Handoff server flows, owner locking, queue
policy, and the SQLite concurrency regression.

### Shared JavaScript client

```bash
node --test tests/js/emo_strict_v2_client.test.js
```

Result: PASS, 13 tests.

### Legacy and strict server profiles

```bash
python -m unittest \
  tests.emo_legacy_suite \
  tests.base.test_emo_strict_v2_core \
  tests.base.test_emo_strict_v2_follow \
  tests.base.test_emo_strict_v2_handoff \
  tests.base.test_emo_strict_v2_broadcast
```

Result: PASS, 174 tests.

### Complete Python suite

```bash
python -m unittest
```

Result: PASS, 1,215 tests in 297.318 seconds (`skipped=3`).

### Documentation and source consistency

```bash
cd docs
sphinx-build -M html . _build

cd ..
node --check supysonic/static/js/emo_strict_v2_client.js
node --check tests/browser/emo_web_strict_v2_acceptance.js
git diff --check
```

Result: PASS.

## Distribution artifacts

Built with:

```bash
python -m build --no-isolation \
  --outdir /tmp/supysonic-strict-v2-dist-20260713-final3
```

Result: PASS.

| Artifact | SHA-256 |
| --- | --- |
| `emosonic_server-0.8.0-py3-none-any.whl` | `367fedd9e5103cef1f1d24ae1a83317251ee6f4bb42e412f5cb797055bb428c4` |
| `emosonic server-0.8.0.tar.gz` | `4f999245dec3112777dae31cf92d37ca50cc7ca00b91e238ffb3f2d37767b18a` |

Direct wheel and sdist listings both contain:

- `supysonic/emo/browser_auth.py`;
- `supysonic/static/js/emo_strict_v2_client.js`;
- `supysonic/templates/player_strict_v2.html`;
- `supysonic/templates/control_strict_v2.html`.

Each archived file's SHA-256 matches the corresponding source hash recorded
above. The sdist path also exercised generated man-page packaging.

## Definition of Done audit

| Items | Result | Evidence or boundary |
| --- | --- | --- |
| 1-10 | PASS | Shared client, OTP authentication, strict metadata/provenance, forbidden-field/action checks, Context/control/feedback/reconnect, and explicit error UI are covered by Python, Node, and browser runs. |
| 11 | PASS | Two real browser players completed Broadcast start, play/pause, seek, queue sync, and stop with negotiated Broadcast capability. |
| 12 | PASS | Follow was initiated by the real follower player, used authority device feedback, advanced continuously without periodic backward seeks, and cleaned up on source network loss. |
| 13 | PASS | Prepare rejection paths, strict commit, real audio start, complete, release, authority transfer, and 30 timing samples passed with paired Handoff capabilities. |
| 14-16 | PASS | Handoff capability pairing is tested, strict mode never falls back automatically, and legacy server/profile tests remain green. |
| 17 | PASS | Targeted Python, Node, full unittest, and the Chromium/Firefox/mobile browser matrix all pass. |
| 18 | PASS | Base commit, contract, runtime versions, browser versions, commands, source hashes, artifact hashes, and timing evidence are frozen here. |
| 19 | PASS | Web/Flutter legacy examples are labeled and the strict Web documentation is included in Sphinx. |
| 20 | PASS WITH INDEPENDENT REVIEW BOUNDARY | This Goal does not convert `local-test-only:` evidence into production readiness. Runtime and packaged probes now reject that evidence outside explicit Flask test mode. |
| 21-23 | PASS | Single-owner player identity, queue close/new-ID and duplicate rejection, and explicit strict/legacy rendering and rollback all have automated and browser evidence. |

## Independent readiness boundary

The isolated command:

```bash
python script/verify_emo_strict_v2_packaging.py
```

builds and installs the wheel and sdist, then exits `0` with:

```text
Strict-v2 packaging verification passed for wheel and sdist; installed missing/invalid/hash-mismatched manifests fail closed.
```

The preserved user-owned `supysonic/emo/strict_v2_conformance.json` still uses
`local-test-only:` evidence and was not rewritten. The loader rejects that
evidence on production and installed-runtime paths; only the Flask test-mode
registration path opts in explicitly. Formal manifest/conformance review
remains separate.

## Out-of-scope follow-up

- Review or replace the packaged `local-test-only:` conformance evidence through
  the independent readiness process.
- Decide production rollout timing and whether to enable the Web strict-v2 and
  optional-profile gates outside the isolated acceptance environment.
- Remove the legacy Web client only under a separate post-rollout Goal.
