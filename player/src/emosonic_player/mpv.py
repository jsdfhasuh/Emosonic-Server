"""Bounded, private mpv IPC. Media URLs never enter argv or diagnostics."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile


class MpvError(RuntimeError):
    """Safe error category; deliberately excludes mpv response/URL text."""


@dataclass(frozen=True)
class PlaybackEvent:
    kind: str
    run_id: str | None
    process_generation: int


class MpvPlayer:
    def __init__(self, audio_device: str, runtime_dir: str | Path,
                 executable: str = "mpv", timeout: float = 10.0,
                 *, allow_null_output_for_tests: bool = False):
        if (not audio_device or audio_device == "auto"
                or (audio_device == "null" and not allow_null_output_for_tests)):
            raise ValueError("an explicit audio device is required")
        self.audio_device = audio_device
        self.runtime_dir = Path(runtime_dir)
        self.executable = executable
        self.timeout = timeout
        self.events: asyncio.Queue[PlaybackEvent] = asyncio.Queue()
        self._generation = 0
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._entries: dict[int, str] = {}
        self._run_id: str | None = None
        self._entry: int | None = None
        self._event_entry: int | None = None
        self._loaded: set[int] = set()
        self._process = None
        self._reader_task = None
        self._writer = None
        self._private_dir: Path | None = None
        self._operations = asyncio.Lock()
        self._closing = False

    @property
    def process_generation(self) -> int:
        return self._generation

    async def start(self) -> None:
        if self._process is not None:
            return
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._private_dir = Path(tempfile.mkdtemp(prefix="mpv-", dir=self.runtime_dir))
        os.chmod(self._private_dir, 0o700)
        socket = self._private_dir / "ipc"
        self._generation += 1
        self._closing = False
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.executable, "--idle=yes", "--pause=yes", "--no-video",
                "--no-config", "--load-scripts=no", "--terminal=no",
                "--input-default-bindings=no", "--input-terminal=no",
                "--audio-fallback-to-null=no",
                *( ["--ao=null"] if self.audio_device == "null" else
                   ["--audio-device=" + self.audio_device] ),
                "--input-ipc-server=" + str(socket),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            deadline = asyncio.get_running_loop().time() + self.timeout
            while True:
                if self._process.returncode is not None:
                    raise MpvError("mpv_start_failed")
                try:
                    reader, self._writer = await asyncio.open_unix_connection(
                        str(socket), limit=1024 * 1024)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise MpvError("mpv_start_timeout")
                    await asyncio.sleep(0.02)
            self._reader_task = asyncio.create_task(self._read(reader, self._generation))
            await self._command(["get_property", "mpv-version"])
        except (OSError, MpvError, asyncio.CancelledError):
            await self.close()
            raise MpvError("mpv_start_failed") from None

    async def _read(self, reader: asyncio.StreamReader, generation: int) -> None:
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid message")
                if generation != self._generation:
                    return
                if "request_id" in message:
                    future = self._pending.pop(message["request_id"], None)
                    if future is not None and not future.done():
                        if message.get("error") == "success":
                            future.set_result(message.get("data"))
                        else:
                            future.set_exception(MpvError("mpv_command_failed"))
                else:
                    self._event(message, generation)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        finally:
            if generation == self._generation and not self._closing:
                run_id = self._run_id
                self._invalidate()
                self._fail_pending("mpv_disconnected")
                self.events.put_nowait(PlaybackEvent("disconnected", run_id, generation))
                # A lost IPC channel must not leave unmanageable audio playing.
                if self._process is not None and self._process.returncode is None:
                    self._process.kill()
                    await self._process.wait()

    def _event(self, message: dict, generation: int) -> None:
        event = message.get("event")
        if event == "start-file":
            self._event_entry = message.get("playlist_entry_id")
        elif event == "file-loaded" and isinstance(self._event_entry, int):
            self._loaded.add(self._event_entry)
        elif event == "end-file":
            entry = message.get("playlist_entry_id")
            run = self._entries.pop(entry, None)
            self._loaded.discard(entry)
            if run is None or run != self._run_id or entry != self._entry:
                return
            self._invalidate()
            reason = message.get("reason")
            if reason in ("eof", "error"):
                self.events.put_nowait(PlaybackEvent(reason, run, generation))

    def _invalidate(self) -> None:
        self._run_id = None
        self._entry = None
        self._entries.clear()
        self._loaded.clear()

    def _fail_pending(self, category: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MpvError(category))
        self._pending.clear()

    async def _command(self, command: list):
        if self._writer is None or self._writer.is_closing():
            raise MpvError("mpv_not_connected")
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._writer.write(json.dumps({"command": command, "request_id": request_id}).encode() + b"\n")
            await asyncio.wait_for(self._writer.drain(), self.timeout)
            return await asyncio.wait_for(future, self.timeout)
        except (TimeoutError, asyncio.CancelledError, OSError):
            # Closing/killing the owned process also fences a late successful seek/load.
            await self.close()
            raise MpvError("mpv_command_interrupted") from None
        finally:
            self._pending.pop(request_id, None)

    async def _until(self, predicate) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout
        while not await predicate():
            if asyncio.get_running_loop().time() >= deadline:
                await self.close()
                raise MpvError("mpv_state_timeout")
            await asyncio.sleep(0.02)

    @asynccontextmanager
    async def _operation(self):
        async with self._operations:
            try:
                yield
            except (MpvError, asyncio.CancelledError):
                await self.close()
                raise

    async def load(self, url: str, run_id: str, position_ms: float = 0) -> None:
        if not run_id or not math.isfinite(position_ms) or position_ms < 0:
            raise ValueError("invalid playback identity or position")
        async with self._operation():
            self._invalidate()
            try:
                await self._command(["set_property", "pause", True])
                await self._command(["loadfile", url, "replace"])
                playlist = await self._command(["get_property", "playlist"])
                if not isinstance(playlist, list):
                    raise MpvError("mpv_load_identity_missing")
                current = [item for item in playlist if isinstance(item, dict) and item.get("current")]
                if len(current) != 1 or not isinstance(current[0].get("id"), int):
                    raise MpvError("mpv_load_identity_missing")
                self._entry = current[0]["id"]
                self._run_id = run_id
                self._entries[self._entry] = run_id

                async def loaded():
                    return self._entry is not None and self._entry in self._loaded
                await self._until(loaded)
                if position_ms:
                    await self._seek(position_ms)
                if not await self._command(["get_property", "pause"]):
                    raise MpvError("mpv_load_not_paused")
            except (MpvError, asyncio.CancelledError):
                await self.close()
                raise

    async def play(self) -> None:
        async with self._operation():
            self._require_run()
            await self._command(["set_property", "pause", False])
            async def playing():
                self._require_run()
                return (not await self._command(["get_property", "pause"])
                        and not await self._command(["get_property", "paused-for-cache"])
                        and not await self._command(["get_property", "core-idle"]))
            await self._until(playing)

    async def pause(self) -> None:
        async with self._operation():
            await self._command(["set_property", "pause", True])
            if not await self._command(["get_property", "pause"]):
                await self.close()
                raise MpvError("mpv_pause_unconfirmed")

    async def seek(self, position_ms: float) -> None:
        async with self._operation():
            await self._seek(position_ms)

    async def _seek(self, position_ms: float) -> None:
        self._require_run()
        if not math.isfinite(position_ms) or position_ms < 0:
            raise ValueError("invalid seek position")
        await self._command(["seek", position_ms / 1000, "absolute+exact"])
        async def reached():
            self._require_run()
            seeking = await self._command(["get_property", "seeking"])
            position = await self._command(["get_property", "time-pos"])
            return not seeking and isinstance(position, (int, float)) and abs(position * 1000 - position_ms) <= 250
        await self._until(reached)

    async def set_volume(self, percent: float) -> None:
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError("volume must be between 0 and 100")
        async with self._operation():
            await self._command(["set_property", "volume", percent])
            actual = await self._command(["get_property", "volume"])
            if not isinstance(actual, (int, float)) or abs(actual - percent) > 0.1:
                raise MpvError("mpv_volume_unconfirmed")

    async def stop(self) -> None:
        async with self._operation():
            self._invalidate()
            await self._command(["stop"])
            async def stopped():
                return await self._command(["get_property", "idle-active"]) is True
            await self._until(stopped)

    def _require_run(self) -> None:
        if self._run_id is None:
            raise MpvError("mpv_no_active_run")

    async def snapshot(self) -> dict:
        async with self._operation():
            run = self._run_id
            result = {"run_id": run, "process_generation": self._generation}
            for prop, key in (("time-pos", "position_ms"), ("duration", "duration_ms"),
                              ("pause", "paused"), ("paused-for-cache", "buffering"),
                              ("volume", "volume")):
                try:
                    value = await self._command(["get_property", prop])
                except MpvError as error:
                    if str(error) != "mpv_command_failed":
                        raise
                    value = None
                if key.endswith("_ms") and isinstance(value, (int, float)):
                    value *= 1000
                result[key] = value
            if run != self._run_id:
                raise MpvError("mpv_snapshot_superseded")
            return result

    async def close(self) -> None:
        self._closing = True
        self._invalidate()
        self._fail_pending("mpv_closed")
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
        if self._process is not None:
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), 2)
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            self._process = None
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._reader_task = None
        if self._private_dir is not None:
            (self._private_dir / "ipc").unlink(missing_ok=True)
            self._private_dir.rmdir()
            self._private_dir = None
