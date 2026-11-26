# Cross-controller replacement queue regression

An existing speaker queue must be replaceable by one controller command. The speaker must play the selected item from the replacement queue. A separate controller must be able to pause that same item without changing the queue or the controlling computer's own playback context.

Run the player implementation and a real mpv process with its explicit test-only null audio output against a disposable server containing the optional `queue.playItem.queueSongIds` contract. Use independent Socket.IO connections, a new SQLite database, synthetic WAV tracks and authenticated Subsonic streams. Compare the canonical queue version, order, active item and committed transport state from both controllers with the speaker, and check the stream track ID and pause state directly through mpv IPC.

This cross-branch test is opt-in. Set `SERVER` and `PLAYER` to the absolute paths of the server and player checkouts and `LAB` to a new private directory. Use Python 3.11 or later with the server's Python dependencies in the first shell and the player's dependencies and mpv in the second. Both processes must share the same loopback interface. The fixture creates its database, user and WAVs under `LAB`. Stop the fixture after the test.

```sh
PYTHONPATH="$SERVER:$PLAYER/player/src" python "$PLAYER/player/tests/test_server_integration.py" --serve-queue-fixture "$LAB/fixture.json"
PYTHONPATH="$PLAYER/player/src" EMOSONIC_QUEUE_FIXTURE="$LAB/fixture.json" python "$PLAYER/player/tests/test_server_integration.py" LiveRemoteQueueTests -v
```

For isolated containers, share a private network namespace with no external interface and share only the fixture manifest directory. No sound device, production configuration or existing database is needed.

## Verification on 2026-11-24

Using the player from `260c7cc`, the existing server `c952ff5` played the original track but rejected the replacement command with `bad_request`. The same test passed with server `a8563f6ac683b865f8539832359439cc5dfaee18` in 3.593 seconds. The three sessions authenticated under the disposable account using ordinary player/controller registration. Both controllers, the speaker's executed context and mpv agreed on the selected track and transport state.

| Operation | Queue revision | Control version | Index | Synthetic track | mpv paused |
| --- | --- | --- | --- | --- | --- |
| Play original | 3 | 3 | 0 | `57ee9294-2f16-4f9f-a7dd-02e013785626` | false |
| Replace and play from desktop | 4 | 4 | 1 | `f9b29fa6-2d21-4a36-a01e-b06528863b84` | false |
| Pause from independent controller | 4 | 5 | 1 | `f9b29fa6-2d21-4a36-a01e-b06528863b84` | true |
