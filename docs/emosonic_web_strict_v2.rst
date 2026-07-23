EmoSonic strict-v2 web player and control console
==================================================

The ``/player`` and ``/control`` pages can use the PlaybackContext strict-v2
``2.4.0`` protocol. Both pages are switched together with:

.. code-block:: ini

   [webapp]
   emo_web_realtime_protocol = strict_v2

The default remains ``legacy`` so deployment is explicit. A strict-v2 error is
shown in the page and closes that connection; the browser does not resend a
legacy request automatically.

Shared client
-------------

Both strict pages load :file:`supysonic/static/js/emo_strict_v2_client.js`.
It owns the Socket.IO bootstrap state machine, request correlation, retry
fingerprints, registration metadata and provenance checks, canonical Context
cursors, subscriptions, reconnect recovery, and negotiated capability gates.
The page templates retain media and UI behavior only.

Browser authentication
----------------------

An authenticated page requests a short-lived password from
``POST /emo/browser-auth-password`` using its same-origin Cookie and a CSRF
header. The response contains ``userName``, a ``browser-otp:`` one-time
password, and its expiration time. The credential is bound to that user and
browser session, explicitly consumed by the Socket.IO authentication layer,
and never stored in local storage or logged. Each browser session retains only
the configured number of outstanding credentials and evicts the oldest when
that limit is exceeded. Per-session issuance limits and a global active-session
capacity bound memory and request cost. Responses use ``Cache-Control: no-store``.

When TLS terminates at a reverse proxy, the proxy must preserve the public
``Host`` header and overwrite ``X-Forwarded-Proto`` with the external scheme.
The browser credential endpoint uses that scheme for its exact Origin check;
clients must not be allowed to supply an untrusted forwarded-proto value.

Identity and Context discovery
------------------------------

The player keeps stable ``clientId`` and ``deviceSessionId`` values in local
storage and acquires a single-tab owner lock before connecting. The control
console keeps its identity in session storage so independent tabs do not
replace each other.

The strict ``device.list`` response intentionally has no Context identifier.
The control console cross-checks online players with the same-user, no-store
``GET /emo/web-context-bindings`` response before subscribing to a Context.

Feature behavior
----------------

The player creates a Context only for a non-empty queue with unique track IDs,
uses ``queue.context.sync`` with canonical cursors, reports device feedback via
``playback.update`` and closes the Context when the queue is cleared. A later
queue receives a new Context ID.

The control console subscribes and requests status before sending
PlaybackContext player controls. Device volume is independent: the console can
send ``device.setVolume`` to an online player by its exact client/device pair
even when that player has no Context. The player confirms the actual value via
``device.volume.update``; online volume state is cleared on disconnect and is
not persisted. Full remote queue replacement remains unavailable.

Broadcast uses strict snapshots and participant feedback. Follow is initiated
by the actual follower player and uses a 1500 ms drift threshold. Handoff is
offered only to targets that negotiated both ``playbackPrepare`` and
``effectiveAtPlayback``; targets reject prepare when media loading, visibility,
or user-gesture requirements cannot be met.

The optional profile implementations are present but their web registration
flags remain fail-closed: ``supportsBroadcast``, ``supportsFollow``,
``playbackPrepare``, and ``effectiveAtPlayback`` are ``false`` until the
required two-browser checks and foreground Handoff timing samples are recorded.
Core PlaybackContext operation is unaffected.

Acceptance deployments can enable the web capability advertisements with
``emo_web_strict_v2_broadcast_enabled``,
``emo_web_strict_v2_follow_enabled``, and
``emo_web_strict_v2_handoff_enabled``. These settings default to ``off`` and do
not bypass the matching server readiness gates.

When the packaged conformance evidence is marked ``local-test-only:``, a
normally deployed integration server must additionally enable both
``emo_development_mode`` and
``emo_strict_v2_allow_local_test_evidence``. This development-only gate must
remain disabled in production.

Verification
------------

The request inventory lives in
:file:`tests/fixtures/emo_web_strict_v2/requests.json`. Python integration tests
exercise browser OTP, exact web registration and Core routing, Context binding
isolation, page switching, and legacy retention. Node's built-in test runner
executes the real shared JavaScript state machine without third-party npm
dependencies.

Automated verification does not replace the manual Chrome/Firefox/mobile
matrix or the required 30-sample Handoff audio-start timing gate. Those results
must be recorded before enabling the corresponding optional capabilities.
