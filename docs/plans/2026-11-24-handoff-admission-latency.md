# Handoff admission latency

In the home lab the real Windows source committed the selected track. Its playing sample was 1.354 seconds old when the controller sent Handoff start. The service spent 2.326 seconds handling the request and rejected the sample at its two-second eligibility check. Read-only probes of the deployed database found 66–87 ms per new connection and about 1 ms for a query on an open connection.

Trace the authenticated Handoff start through its lifecycle, dispatch barrier and durable transactions. Reuse the existing request-local database connection mechanism for sequential work where repeated connection setup consumes the freshness budget. All source and target identity, locking, lifecycle, authority and timing checks must continue to use their current values at admission and commit.

Measure physical database opens on ordinary Handoff admission. On an isolated loopback service exercise a realistic connection-setup delay with two real mpv players and a distinct controller. Verify a fresh playing source can transfer in both directions and a genuinely expired source remains ineligible. Run the relevant existing race, replay and transaction tests.

The original handler checked three Broadcast fences twice. With a 103 ms delay on every real SQLite connection open and an initial sample age of 1,350 ms, it opened 13 connections before the source check and rejected a 2,687 ms sample. Performing the fence checks once under lock and reusing the connection inside the locked admission reduced the entire request to four opens. The check saw a 1,807 ms sample and returned the preparing acknowledgement in 453 ms. The expired-sample test still rejects a 2,100 ms sample. The Handoff, context status and database contract suites passed 68 tests.
