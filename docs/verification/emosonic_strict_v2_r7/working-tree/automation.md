# EmoSonic strict-v2 r7 working-tree automation evidence

> Collected: 2026-07-15, Asia/Shanghai
>
> Status: non-final working-tree evidence. This directory is intentionally not named after a
> `serverBuildCommit`. It must not be referenced by `strict_v2_conformance.json` and does not
> authorize readiness or production rollout.

## Build and contract identity

- Baseline and current checked-out commit: `d448ab31b1eee7e2d2aa09d63d417b29dbe53be7`.
- Working tree: dirty, with the r7 implementation and tests not yet committed.
- Runtime `serverBuildCommit`: `unknown` because `EMO_SERVER_BUILD_COMMIT` is not configured.
- Protocol version: `2.2.0`.
- Contract SHA-256:
  `7e5402a4c32fb366c3755239e4993ef5634177e7db9748bff83b32926cbd2b1f`.
- Registration schema hash:
  `7f4d58564e8c1257f387a40d7d6e3feb38d831c1b3c232165c14c96f9e80fcec`.
- Working-tree diff at collection time: 39 tracked files changed, 2740 insertions, 313 deletions,
  plus the untracked r7 Goal, evidence collector, provider migrations, verification-script tests,
  discovery fixture, and this evidence directory.
- `core`, `follow`, `handoff`, and `broadcast` all remain
  `codeConformanceReady:false` with empty evidence.

## Automated verification

### Full repository

```text
python -m unittest
Ran 1253 tests in 304.068s
OK (skipped=3)
```

The three skips are the environment-gated PostgreSQL/MySQL migration tests and one existing
environment-specific strict-v2 test. The two migration tests were run separately against isolated
real providers as recorded below.

```text
node --test tests/js/emo_strict_v2_client.test.js
18 tests passed, 0 failed
```

```text
cd docs && make html
build succeeded
```

```text
git diff --check
exit 0
```

### r7 manifest and artifact tooling

```text
python script/verify_emo_strict_v2_ears.py
Running 93 strict-v2 EARS evidence tests
Ran 93 tests in 47.976s
OK
```

The EARS runner required the executable manifest to map every requirement from REQ-001 through
REQ-025 to a concrete test method.

```text
python script/verify_emo_strict_v2_packaging.py
Strict-v2 packaging verification passed for wheel and sdist; installed
missing/invalid/hash-mismatched manifests fail closed.
```

The installed wheel and sdist probes both reported protocol `2.2.0`, the r7 contract hash, and
all-false code readiness from the packaged manifest.

```text
python script/collect_emo_strict_v2_r7_evidence.py \
  --identity-only \
  --server-build-commit d448ab31b1eee7e2d2aa09d63d417b29dbe53be7
Strict-v2 r7 evidence collection failed: Final evidence requires a clean working tree
exit 1
```

This expected failure proves the final collector will not freeze evidence from this dirty
working tree or mislabel the baseline commit as the r7 implementation build.

### Real database providers

The provider tests ran inside a temporary test runner on an isolated Docker network. The database
containers and network were removed after the run; no existing database container or business data
was used.

PostgreSQL provider: `postgres:17-alpine`

```text
SUPYSONIC_TEST_POSTGRES_URI=... python -m unittest \
  tests.base.test_emo_schema_migration.EmoSchemaMigrationTestCase.\
test_postgres_runtime_clean_schema_and_20260708_upgrade
Ran 1 test in 0.713s
OK
```

MySQL provider: `mysql:8.4.5`

```text
SUPYSONIC_TEST_MYSQL_URI=... python -m unittest \
  tests.base.test_emo_schema_migration.EmoSchemaMigrationTestCase.\
test_mysql_runtime_clean_schema_and_20260708_upgrade
Ran 1 test in 4.855s
OK
```

Both tests verified clean schema creation, upgrade from schema `20260708`, data normalization,
schema version `20260715`, and the non-unique discovery index with columns in this exact order:

```text
user_name, lifecycle, authority_client_id, authority_device_session_id
```

## r7-specific failure and race evidence

The executable manifest maps REQ-001 through REQ-025 to concrete test methods. High-risk r7 paths
include:

- send-buffer reservation failure disconnects only the stale binding-event recipient;
- an actual Socket emit exception disconnects that recipient, preserves the mutation, and allows a
  reconnect followed by canonical `playback.context.list` recovery;
- create/close/Handoff complete expose internal `mutated`, `affected_authority_pairs`, and
  `canonical_context` metadata without changing wire responses;
- create/control, close/control, and Handoff/control races use barriers, repeat ten times, and admit
  only the two contract-allowed linearization outcomes;
- all six normal controls return a complete cursor conflict without database, cursor, or authority
  inbox side effects when an authority/device pair is ambiguous;
- the discovery fixture records both list-response/binding-event arrival orders and requires a local
  client `discoveryGeneration`; that generation is explicitly not a wire field.

## Evidence still required before freeze

This working-tree evidence does not satisfy the final r7 freeze. The following remain external or
post-commit requirements:

1. Produce a unique committed `serverBuildCommit` and rerun the evidence under a directory named
   for that commit.
2. Record Android and Windows scenarios A through E with aligned request IDs, Context IDs, cursors,
   Flutter build ID, server build commit, and r7 contract hash.
3. Re-freeze Core, Follow, Handoff, and Broadcast evidence against that exact build and contract.
4. Only after all acceptance evidence passes, update conformance readiness; production rollout
   remains a separate approval.
