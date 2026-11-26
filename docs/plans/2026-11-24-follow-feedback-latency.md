# Keep ordinary playback facts fresh for Follow

Windows emits a playing sample every second. In the observed session, many samples were already more than two seconds old when persisted, so Follow admission correctly refused them and an existing follower waited for a fresh sample.

Measure the strict playback.update path on an isolated server, including common ingress, database connections, serialized work and publication. Remove the demonstrated bottleneck while preserving the ordinary update transaction, authentication, cursor/nonce fences, same-context ordering, and the two-second source freshness limit. Check profile admission separately where measurements show significant time before its freshness check.

Exercise ordinary progress with concurrent source and follower activity, stale feedback, and pre-existing control/Handoff semantics. Compare the same input before and after the change. The bench measures the exercised path; field verification remains separate.

## Verification

The Socket.IO/SQLite fixture charged 103 ms per real database connection, the handshake cost observed in the lab. With one source, ordinary progress went from 8 connections and 875 ms to 1 and 125 ms. Follow admission went from 9 and 969 ms to 1 and 125 ms. Broadcast admission went from 14 and 1562 ms to 1 and 141 ms. These wall times include the injected delay.

The isolated regressions cover valid and expired facts and a progress update arriving while a new follower holds the admission locks. The follower receives that update after its subscription takes effect.
