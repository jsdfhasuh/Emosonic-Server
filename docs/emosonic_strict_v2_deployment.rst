Emosonic strict-v2 deployment
=============================

Public deployments must terminate TLS in the application server or a trusted
reverse proxy. Clients must use ``https://`` and ``wss://`` externally; plain
HTTP/WebSocket transport is only suitable for an isolated local development
environment.

Origin policy
-------------

Production defaults to same-origin Socket.IO requests. Set
``emo_allowed_origins`` in the ``WEBAPP`` configuration section to a
comma-separated list of exact trusted origins when clients are hosted on other
origins. A wildcard is rejected unless both ``emo_allowed_origins=*`` and
``emo_development_mode=true`` are explicitly configured; development wildcard
mode emits a security warning.

Profile readiness
-----------------

Runtime capability negotiation is the intersection of server implementation
support, the matching deployment switch, the capability requested by the
client, and its role/execution abilities. Contract hashes, schema hashes,
build commits, conformance manifests, and evidence files do not participate in
connection or capability decisions.

All deployment profile switches default to off. Enable only the profiles used
by that deployment:

.. code-block:: ini

   [webapp]
   emo_strict_v2_core_enabled = on
   emo_strict_v2_follow_enabled = on
   emo_strict_v2_handoff_enabled = on
   emo_strict_v2_broadcast_enabled = on

Turning a profile off rejects new operations for that profile. Existing
Broadcast terminal restore work continues until every frozen participant pair
has completed recovery.

Runtime limits
--------------

The defaults are 10 unauthenticated connections per IP, 20 authenticated
connections per user, 120 strict requests per connection per minute, 20 player
controls per connection per second, and 10 starts per minute for each of
Context create, Handoff, and Broadcast. Deployments may lower these values.
Raising one requires a non-empty ``emo_strict_rate_limit_load_test_evidence``
reference to reviewed load-test evidence.

The Engine.IO message limit is 256 KiB. Ping interval and timeout default to 25
and 20 seconds and can be lowered with ``emo_socketio_ping_interval`` and
``emo_socketio_ping_timeout``.

Process model and shutdown
--------------------------

When strict realtime Core is code-conformant and deployment-enabled, the server
must run exactly one process. ``supysonic-server --processes 2`` fails before
startup in that state. Thread concurrency remains supported, and Context
mutations are serialized by resource.

Gunicorn begins a graceful realtime drain on worker interrupt: new connections
and new strict requests are rejected, in-flight requests receive up to
``emo_strict_shutdown_grace_seconds`` (default 5) to settle, then remaining
Socket.IO connections are closed. Persistent Context and tombstone state is
rehydrated on the replacement worker; transient profiles are terminated by the
startup reconciliation rules.
