# Coherent PlaybackContext status snapshots

A valid Follow preflight status request can capture runtime Context cursor 5,
then race a real local-user mutation that commits Context/device cursor 6.
The status builder independently reads the newer device fact and tries to emit
it with cursor 5. The unchanged strict output validator correctly rejects this
mixed snapshot; the request handler reports bad_request to the native client.

Use a durable Context and device-state snapshot collected under the existing
per-Context serialization lock and read transaction, using a single LEFT JOIN
SELECT. This gives statement consistency on PostgreSQL READ COMMITTED as well
as SQLite even for legacy writers outside the strict Context lock. The current
strict server requires one process; the same Context lock spans collection of
in-memory transient facts. Recheck ownership
and lifecycle from that durable Context, never from an untrusted request value.

Hold the same Context lock while collecting any runtime-only transient facts.
Only current registered owner/client/device and matching Context-epoch facts
may supplement the durable snapshot. Preserve newer in-scope observations; do
not clamp or hide applied cursors beyond the Context or weaken strict output
validation. Do not replace a newer durable fact with an older runtime one.

Verify deterministic status/mutation interleaving using real DB operations and
Socket.IO requests, direct status and push/subscription flows, runtime transient
scope, closed/missing/foreign Context responses, and existing Handoff/Follow and
strict Core regressions. Native testing remains separate from protocol evidence.

No production deployment, latest image mutation, or PR-description changes.
