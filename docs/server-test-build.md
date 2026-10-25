# Emosonic Server TEST build

This is a disposable test build, not a formal release or production deployment.
The TEST branch is `test/server-follow-actual-rate-20261025`. Product source is
exactly `5adc23da173c5f20188d79aafe8fcc1e656eb38a`, with package version `0.8.0`
unchanged. The build workflow and instructions are separate from that product
source. The existing Dockerfile is built from a clean `git archive` of the source
commit, without checkout credentials, TEST tooling, generated files, or local data.
Wheel and sdist use a second clean archive so test-generated files cannot enter
either build context.

The workflow replaces the inherited all-branch registry publisher on this TEST
branch. It has read-only repository permission and never logs in to a registry,
pushes an image, creates a release, merges to master, or deploys a server.

## Download and inspect

Open this branch's **TEST Server Build** run in GitHub Actions and download
`Emosonic-Server-TEST-0.8.0-5adc23da-linux-amd64-<run ID>`. Artifacts expire after
7 days; download the bundle before then. A diagnostics artifact is not a usable
build. Extract the successful bundle and run:

```sh
sha256sum -c SHA256SUMS
cat BUILD-INFO.json
gzip -dc emosonic-server-TEST-0.8.0-5adc23da-linux-amd64.docker.tar.gz | docker load
```

The image tag is `emosonic-server:test-0.8.0-5adc23da-linux-amd64`, for
**Linux amd64** Docker. It is not `latest`. The wheel and sdist are also included,
with the upstream package version unchanged. Do not install them over a running
production environment. `BUILD-INFO.json` distinguishes source commit, build-tools
commit, workflow run, time, platform, and limitations; `checks/` contains results.
Dependencies and the upstream base image are not locked, so this is source-pinned,
not a claim of byte-for-byte reproducible builds.

## Disposable localhost test

Use Docker on the machine where you want to test. These commands intentionally
override the image's normal `setup.sh` entrypoint, which would also start the daemon.
They run only the real web server, with a fresh SQLite database and cache in tmpfs.
Do not add music-library, existing database, production configuration, or Docker
socket mounts. The test data is discarded when this container stops.

```sh
docker run --rm -d --name emosonic-server-test \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  -p 127.0.0.1:5000:5000 \
  --mount "type=bind,src=$(pwd)/config.server-test.sample,dst=/app/supysonic.conf,readonly" \
  --entrypoint supysonic-server \
  emosonic-server:test-0.8.0-5adc23da-linux-amd64 \
  -S gevent -h 0.0.0.0 -p 5000

# Optional: run the same local auth/registration smoke as CI.
docker exec -i emosonic-server-test python - smoke < verify_server_test_build.py

# Create your own disposable non-admin test login, prompting for its password.
docker exec -it emosonic-server-test supysonic-cli user add test-user
```

Use `http://127.0.0.1:5000` from the client on that same machine. The allowlist is
exactly that origin, not a wildcard. This is intentionally not reachable from a
phone or other computer; it does not configure LAN access, TLS, reverse proxies,
or firewall rules. No music is imported, so this proves connection/auth/registration,
not audio playback. The supplied profile settings are intended for the paired TEST
client's Core, Follow, and Handoff integration. Broadcast stays off.

When finished:

```sh
docker logs --tail 100 emosonic-server-test
docker stop emosonic-server-test
```

## What the configuration changes

Upstream defaults keep `mount_emosonic` and all strict-v2 profile switches off.
The packaged conformance readiness is also false. This TEST-only sample explicitly
enables `mount_emosonic`, Core, Follow, Handoff, `emo_development_mode`, and
`emo_strict_v2_allow_local_test_evidence`. Both development switches are necessary
for this source revision's local integration path. They are not production evidence
and must not be carried into production. No product readiness manifest is changed.
The sample has no credentials or API keys, no daemon/watchers, no online lyrics,
no recommendation agent, and no client release upload endpoint.

## Checks and limits

CI runs the existing `coverage run -m unittest` and
`coverage run -a -m unittest tests.net.suite`, then builds the wheel and sdist.
It checks JSON descriptors, schemas/migrations, templates/static resources and
generated man pages, then imports metadata from independently installed wheel and
sdist outside the source tree. The tests.net suite can contact its existing public
test services; it does not use your music library or credentials.

Docker uses the existing Python 3.11 Alpine Dockerfile. The smoke starts the actual
Gevent server with `--network none`, loopback only, and a disposable tmpfs database.
It checks Engine.IO, creates an ephemeral local test user, authenticates, registers
a strict-v2 player, verifies negotiated Core/Follow/Handoff-related capabilities,
and checks `serverBuildCommit` from the registration response. Test credentials are
not printed or packaged. Production defaults remain off. The original daemon
entrypoint, production database migration, real audio playback, paired-client UI,
and multi-device Follow/Handoff behavior are not covered by this build smoke.

Only a successful run with the usable bundle is a completed build. This document
describes the checks; it is not by itself evidence that a particular run passed.
