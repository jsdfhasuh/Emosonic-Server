# Preserve a stopped queue when the OpenWrt player reconnects

## Problem and expected behavior

When the same player session reconnects to a stopped, nonempty playback context, it currently loads the selected track into mpv paused and reports paused. The queue and device then disagree at the same control version.

Restore this context with mpv stopped. Keep its ordered queue, selection, position and cursors. Report the stopped fact only after mpv confirms idle. An explicit play command should load the selected track at its saved position. A paused queue still loads paused, a previously playing queue still reconnects without starting audio, and an empty context stays idle.

## Implementation and verification

- Preserve the result of the existing mpv stop and idle check. If it fails during reconciliation, close the engine and use the existing reconnect and engine restart path.
- Handle a stopped queue separately from queues that need paused preparation.
- Cover same-session reconciliation, explicit play, other queue states and stop failure. Run the player regression suite and exercise stopped recovery against a loopback server with real mpv and a null audio output.
