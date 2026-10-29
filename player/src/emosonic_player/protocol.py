"""Strict Core Socket.IO transport, separate from playback and server runtime."""
import asyncio
import math
from collections.abc import Callable
import time
import uuid
from urllib.parse import urlsplit

CAPABILITIES = {
    "playbackContextV2": True, "playbackPrepare": False,
    "effectiveAtPlayback": False, "canPlay": True, "canPause": True,
    "canSeek": True, "canSetVolume": True, "supportsFollow": False,
    "supportsBroadcast": False, "remoteVolumeControl": True,
}


class ProtocolError(RuntimeError):
    """Safe error category; never contains server text or credentials."""


class ProtocolClient:
    def __init__(self, server_url, username, password, client_id,
                 device_session_id, device_name, on_event=None,
                 on_disconnect=None, timeout=10, socket_factory=None):
        parsed = urlsplit(server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid server URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTTPS required for non-loopback server")
        self.server_url = server_url.rstrip("/")
        self.username, self._password = username, password
        self.client_id, self.device_session_id = client_id, device_session_id
        self.device_name = device_name
        self.on_event, self.on_disconnect = on_event, on_disconnect
        self.timeout, self._factory = timeout, socket_factory
        self.generation = 0
        self.nonce, self.epoch, self.ready = None, None, False
        self._socket = None
        self._pending = {}
        self._waiters = []
        self._tasks = set()
        self._clock_offset = None
        self._send_lock = asyncio.Lock()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled():
            # Retrieve exceptions without logging possibly sensitive payloads.
            task.exception()

    def _invalidate(self):
        self.ready, self.nonce, self.epoch = False, None, None
        self._clock_offset = None
        for _, future, _ in self._pending.values():
            if not future.done():
                future.set_exception(ProtocolError("connection_lost"))
        self._pending.clear()
        for _, _, future, timer in self._waiters:
            timer.cancel()
            if not future.done():
                future.set_exception(ProtocolError("connection_lost"))
        self._waiters.clear()

    async def connect(self):
        if self._socket is not None:
            await self.close()
        self.generation += 1
        generation = self.generation
        self._invalidate()
        if self._factory is None:
            import socketio
            self._socket = socketio.AsyncClient(reconnection=False, logger=False,
                                                engineio_logger=False)
        else:
            self._socket = self._factory()
        async def receive(message):
            if generation == self.generation:
                self._receive(message)
        async def disconnected(*_):
            if generation == self.generation:
                self._invalidate()
                for task in list(self._tasks):
                    task.cancel()
                if self.on_disconnect:
                    self._spawn(self.on_disconnect())
        self._socket.on("message", handler=receive, namespace="/emo")
        self._socket.on("disconnect", handler=disconnected, namespace="/emo")
        try:
            await self._socket.connect(self.server_url, namespaces=["/emo"],
                                       socketio_path="emo/ws", wait_timeout=self.timeout)
            auth = await self.request("auth.login", {"u": self.username, "p": self._password}, type="auth")
            if auth["payload"].get("authenticated") is not True:
                raise ProtocolError("authentication_failed")
            registration = await self.request("device.register", {
                "clientId": self.client_id, "deviceSessionId": self.device_session_id,
                "deviceName": self.device_name, "roles": ["player", "controller"],
                "capabilities": dict(CAPABILITIES),
            }, type="device")
            # The receive path sets provenance before any following events arrive.
            self._validate_registration(registration)
            best = None
            for _ in range(3):
                before = time.monotonic() * 1000
                pong = await self.request("system.ping", {}, type="system", reply_actions={"system.pong"})
                after = time.monotonic() * 1000
                sample = pong["payload"].get("serverTimeMs")
                if type(sample) not in {int, float} or not math.isfinite(sample) or sample <= 0:
                    raise ProtocolError("invalid_clock_sample")
                rtt = after - before
                if best is None or rtt < best[0]:
                    best = (rtt, sample - (before + after) / 2)
            self._clock_offset = best[1]
            self.ready = True
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("connection_failed") from None

    def _validate_registration(self, message):
        p = message.get("payload", {})
        metadata = p.get("strictV2", {})
        if (p.get("action") != "device.register" or p.get("clientId") != self.client_id
                or p.get("deviceSessionId") != self.device_session_id
                or metadata.get("protocolVersion") != "2.9.0"
                or type(metadata.get("connectionEpoch")) is not int
                or metadata["connectionEpoch"] != 1
                or not isinstance(metadata.get("connectionNonce"), str)
                or not metadata["connectionNonce"]):
            raise ProtocolError("registration_mismatch")
        caps = p.get("negotiatedCapabilities")
        if (not isinstance(caps, dict) or set(caps) != set(CAPABILITIES)
                or any(type(v) is not bool for v in caps.values())
                or caps != CAPABILITIES):
            raise ProtocolError("capability_mismatch")
        nonce = metadata["connectionNonce"]
        if message.get("connectionNonce", nonce) != nonce or message.get("connectionEpoch", 1) != 1:
            raise ProtocolError("registration_provenance_mismatch")
        self.nonce, self.epoch = nonce, 1

    def server_time_ms(self):
        if self._clock_offset is None:
            raise ProtocolError("clock_not_ready")
        return int(time.monotonic() * 1000 + self._clock_offset)

    async def request(self, action, payload, type="command", reply_actions=None):
        if self._socket is None or not self._socket.connected:
            raise ProtocolError("not_connected")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        allowed = set(reply_actions or {"system.ack"}) | {"system.error"}
        self._pending[request_id] = (action, future, allowed)
        try:
            async with self._send_lock:
                await self._socket.emit("message", {"type": type, "action": action,
                    "requestId": request_id, "payload": payload}, namespace="/emo")
            return await asyncio.wait_for(future, self.timeout)
        except asyncio.TimeoutError:
            raise ProtocolError("request_timeout") from None
        except asyncio.CancelledError:
            raise
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("send_failed") from None
        finally:
            self._pending.pop(request_id, None)

    async def emit(self, action, payload, type="event"):
        """For canonical-only replies, install expect_event before emitting."""
        if self._socket is None or not self._socket.connected:
            raise ProtocolError("not_connected")
        request_id = uuid.uuid4().hex
        async with self._send_lock:
            await self._socket.emit("message", {"type": type, "action": action,
                "requestId": request_id, "payload": payload}, namespace="/emo")
        return request_id

    async def send_confirmed(self, action, payload, confirmation_action,
                             predicate, type="event"):
        """Wait for canonical proof, failing immediately on correlated rejection.

        An ACK is deliberately not completion. A canonical event can precede it.
        """
        if self._socket is None or not self._socket.connected:
            raise ProtocolError("not_connected")
        canonical = self.expect_event(confirmation_action, predicate)
        request_id = uuid.uuid4().hex
        rejected = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (action, rejected, {"system.error"})
        try:
            async with self._send_lock:
                await self._socket.emit("message", {"type": type, "action": action,
                    "requestId": request_id, "payload": payload}, namespace="/emo")
            completed, _ = await asyncio.wait(
                {canonical, rejected}, return_when=asyncio.FIRST_COMPLETED)
            if rejected in completed:
                return rejected.result()
            return canonical.result()
        except asyncio.CancelledError:
            raise
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("send_failed") from None
        finally:
            self._pending.pop(request_id, None)
            for future in (canonical, rejected):
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    def expect_event(self, action, predicate: Callable = lambda message: True, timeout=None):
        """Register before send; await the returned Future, even if event races ACK."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def expired():
            if not future.done():
                future.set_exception(ProtocolError("canonical_timeout"))
        timer = loop.call_later(self.timeout if timeout is None else timeout, expired)
        waiter = (action, predicate, future, timer)
        self._waiters.append(waiter)
        def cleanup(_):
            timer.cancel()
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        future.add_done_callback(cleanup)
        return future

    def _receive(self, message):
        if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
            return
        action = message.get("action")
        pending = self._pending.get(message.get("requestId"))
        if self.nonce is not None and (message.get("connectionNonce") != self.nonce or message.get("connectionEpoch") != self.epoch):
            return
        if pending and action in pending[2]:
            original, future, _ = pending
            if future.done():
                return
            if action in {"system.ack", "system.error"} and message["payload"].get("action") != original:
                future.set_exception(ProtocolError("reply_action_mismatch"))
            elif action == "system.error":
                code = message["payload"].get("code", "server_error")
                # Do not trust free-form server error text (may echo URL/token).
                known = {"unauthorized", "bad_request", "not_found", "conflict",
                         "forbidden", "rate_limited", "authority_offline",
                         "restore_in_progress", "internal_error", "core_not_ready",
                         "strict_v2_core_not_ready"}
                future.set_exception(ProtocolError(code if code in known else "server_error"))
            else:
                try:
                    if original == "device.register":
                        self._validate_registration(message)
                    future.set_result(message)
                except ProtocolError as exc:
                    future.set_exception(exc)
            return
        if self.nonce is None:
            return
        for expected, predicate, future, _ in list(self._waiters):
            if expected == action and not future.done():
                try:
                    matched = predicate(message)
                except (KeyError, TypeError, ValueError):
                    matched = False
                if matched:
                    future.set_result(message)
        if self.on_event:
            self._spawn(self.on_event(message))

    async def close(self):
        self.generation += 1
        self._invalidate()
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await socket.disconnect()
            except Exception:
                pass  # cleanup cannot expose exception text containing endpoint data
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
