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

Development conformance evidence
--------------------------------

Normal deployments reject conformance evidence prefixed with
``local-test-only:`` even when the matching profile deployment switches are
enabled. This prevents test results from silently becoming production
readiness.

An isolated development deployment may explicitly enable external Strict V2
client integration with both settings:

.. code-block:: ini

   [webapp]
   emo_development_mode = on
   emo_strict_v2_allow_local_test_evidence = on

The local-evidence switch is ignored unless development mode is also enabled,
and enabling it emits a startup warning. It affects code-readiness evaluation
only; the required ``emo_strict_v2_*_enabled`` profile switches must still be
enabled independently. Disable the local-evidence switch after collecting the
fixed-build evidence, and never use it for a production rollout.

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
