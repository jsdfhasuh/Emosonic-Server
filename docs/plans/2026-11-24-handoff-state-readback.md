# Keep ordinary playback feedback current after Handoff

After a target completes a Handoff, its in-memory completion fact can outlive subsequent committed pause and seek updates. Status must return the newest fact for the current physical connection, including for existing normal-mode SQLite records whose connection nonce is present and whose connection epoch was omitted.

Preserve the existing context epoch, account, device pair and current physical generation checks. Strict-v2 uses connection epoch 1 and a new nonce for each physical connection. Compare applied control version and client sequence only for facts from that same generation. Keep the existing replacement behavior on reconnect.

Verify through the Socket.IO status handler and SQLite, with a cached Handoff completion followed by newer ordinary playback feedback. Exercise stale and current nonces, older records and equal control versions. Run the status snapshot suite, related Handoff coverage and the isolated double-Handoff/pause/seek readback.
