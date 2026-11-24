# Replace and play an ordinary remote authority queue

The wire contract is documented in the [lab addendum](../../specs/2026-11-24-remote-queue-replacement.md).

An ordinary controller can select an item from the authority's existing queue.
Selecting a different list must target that same authority without changing the
controller's queue or starting the authority's previous track.

Extend the closed controller request for `queue.playItem` with optional
`queueSongIds`. Require the existing context binding, controller and player
roles, routable physical generation, queue and control cursors, a bounded unique
list, and an index into that list. Keep requests without this field unchanged.
The routed command already carries the full ordered list and index.

Reserve the control cursor and stage the proposed queue in the durable control
transaction. Publish the queue, index, and playback selection atomically after
the bound player reports the selected track playing at the command version.
An ACK means acceptance. Failures, timeouts and superseding local choices
retain the previous queue. Fence conflicting pending mutations and keep the
existing recovery and request replay paths. Old servers reject the extra field.

Exercise the strict request schema and the real Socket.IO transaction with empty
and nonempty target queues, stale cursors, wrong feedback, failed execution,
timeout, competing queue changes, and replays. Verify that existing ordinary
controls and the routed player command keep their original contracts.
