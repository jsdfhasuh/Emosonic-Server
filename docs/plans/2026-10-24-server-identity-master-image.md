# Server identity master image

## Scope

The user requests merging the Base64 server identity feature into server master
and building a new image. Feature commit c562550 is not included in the deployed
master image 859c5dc. No OpenWrt container update, restart, configuration change,
or live database mutation is authorized by this task.

## Integration

- Merge c562550 into origin/master at 859c5dc without discarding newer master
  changes, especially the linux/amd64 and linux/arm64 Docker publishing workflow.
- Preserve normal ping responses. Authenticated requests with
  includeServerIdentity=true return version, a persistent 32-byte Base64 identity,
  and the existing immutable account ID. Retain existing Meta storage; no schema
  addition, credential change, or identity rotation is introduced.
- Commit and push master without force. The existing Docker Publish push trigger
  must test and build before publishing latest, master, and the commit SHA tag.
- Verify the Actions source SHA, successful tests/build, and the published image
  index including linux/arm64. Do not use the local Docker daemon or deploy to the
  runtime host as part of verification.

## Validation

- Run tests.api.test_system using the existing local supysonic Python environment;
  its fixtures use temporary SQLite databases, not the runtime database.
- Verify the integration changes only the identity implementation/API/tests and
  this plan, with no workflow modifications or merge conflicts.
- Let existing CI run the complete Python and network suites and package checks.
- Report build and registry publication separately from runtime deployment.

Pre-push results: all 8 system API tests passed. Merge completed without
conflicts; the Docker workflow matches origin/master unchanged. Integration is
limited to three feature files and this plan. CI and runtime acceptance are not
implied by these local results.

## Later Runtime Acceptance

After a separately authorized deployment, authenticated ping from the configured
LAN/relay addresses must return the same server ID and account ID for the same
account. A different account must retain the server ID but return its own account
ID. Identity must persist across restarts, and normal ping must remain compatible.
Never print credentials or silently regenerate a corrupt stored identity. A
matching public Base64 ID is a routing identity, not cryptographic authentication.
