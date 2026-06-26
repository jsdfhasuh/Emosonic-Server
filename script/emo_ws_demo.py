#!/usr/bin/env python3
import argparse
import json
import sys
import threading
import time

import socketio


def build_message(msg_type, action, payload=None, request_id=None, target_client_id=None):
    message = {
        "type": msg_type,
        "action": action,
        "payload": payload or {},
        "timestamp": time.time(),
    }
    if request_id is not None:
        message["requestId"] = request_id
    if target_client_id is not None:
        message["targetClientId"] = target_client_id
    return message


def send_message(sio, message):
    payload = json.dumps(message, ensure_ascii=True)
    print(">>>", payload)
    sio.emit("message", message, namespace="/emo")


def authenticate(sio, args):
    send_message(
        sio,
        build_message(
            "auth",
            "auth.login",
            {"u": args.user, "p": args.password},
            request_id="auth-1",
        ),
    )


def register_device(sio, args):
    payload = {
        "clientId": args.client_id,
        "deviceName": args.device_name,
        "roles": args.roles,
        "sessionId": args.session_id,
    }
    if args.alias:
        payload["alias"] = args.alias

    send_message(
        sio,
        build_message(
            "device",
            "device.register",
            payload,
            request_id="register-1",
        ),
    )


def request_device_list(sio):
    send_message(
        sio,
        build_message("device", "device.list", {}, request_id="device-list-1"),
    )


def is_ack(message, request_id):
    return (
        message.get("action") == "system.ack"
        and message.get("requestId") == request_id
    )


def build_client(args, on_ready, on_message):
    sio = socketio.Client(logger=False, engineio_logger=False)
    state = {
        "authenticated": False,
        "registered": False,
        "ready": False,
        "command_sent": False,
        "queue_sent": False,
    }

    @sio.event(namespace="/emo")
    def connect():
        print("connected")
        authenticate(sio, args)

    @sio.event(namespace="/emo")
    def disconnect():
        print("disconnected")

    @sio.on("message", namespace="/emo")
    def message(data):
        print("<<<", json.dumps(data, ensure_ascii=True))
        if is_ack(data, "auth-1"):
            state["authenticated"] = True
            register_device(sio, args)
            return

        if is_ack(data, "register-1"):
            state["registered"] = True
            request_device_list(sio)
            return

        if data.get("action") == "device.list" and state["registered"] and not state["ready"]:
            state["ready"] = True
            on_ready(sio, state)

        on_message(sio, data)

    return sio


def run_player(args):
    playback_state = {
        "sessionId": args.session_id,
        "state": "paused",
        "trackId": args.track_id,
        "positionMs": 0,
        "volume": 70,
    }

    def on_message(sio, message):
        action = message.get("action")
        if action == "player.play":
            playback_state["state"] = "playing"
        elif action == "player.pause":
            playback_state["state"] = "paused"
        elif action == "player.next":
            playback_state["state"] = "playing"
        elif action == "player.prev":
            playback_state["state"] = "playing"
        elif action == "player.seek":
            playback_state["positionMs"] = message.get("payload", {}).get(
                "positionMs", playback_state["positionMs"]
            )
        else:
            return

        send_message(
            sio,
            build_message(
                "event",
                "playback.update",
                playback_state,
                request_id=f"playback-{int(time.time())}",
            ),
        )

    def on_ready(sio, state):
        print("player registered and ready")

    sio = build_client(args, on_ready, on_message)
    try:
        sio.connect(
            args.url,
            namespaces=["/emo"],
            socketio_path="/emo/ws",
        )
        print("player mode started, waiting for commands")
        sio.wait()
    finally:
        sio.disconnect()


def run_controller(args):
    ready_event = threading.Event()

    def on_ready(sio, state):
        print("controller registered and ready")
        if args.queue_json and not state["queue_sent"]:
            with open(args.queue_json, "r", encoding="utf-8") as fh:
                queue_song_ids = json.load(fh)
            send_message(
                sio,
                build_message(
                    "state",
                    "queue.session.sync",
                    {
                        "sessionId": args.session_id,
                        "queueSongIds": queue_song_ids,
                        "currentIndex": 0,
                        "positionMs": 0,
                    },
                    request_id="queue-sync-1",
                ),
            )
            state["queue_sent"] = True
        ready_event.set()

    def on_message(sio, message):
        return None

    sio = build_client(args, on_ready, on_message)
    try:
        sio.connect(
            args.url,
            namespaces=["/emo"],
            socketio_path="/emo/ws",
        )
        if not ready_event.wait(timeout=10):
            raise RuntimeError("Controller did not become ready within 10 seconds")

        print("controller interactive mode")
        print("commands: play, pause, next, prev, seek <ms>, queue <file>, list, quit")

        while True:
            try:
                raw_command = input("> ").strip()
            except EOFError:
                break

            if not raw_command:
                continue
            if raw_command in {"quit", "exit"}:
                break
            if raw_command == "list":
                request_device_list(sio)
                continue
            if raw_command.startswith("queue "):
                queue_path = raw_command.split(" ", 1)[1].strip()
                with open(queue_path, "r", encoding="utf-8") as fh:
                    queue_song_ids = json.load(fh)
                send_message(
                    sio,
                    build_message(
                        "state",
                        "queue.session.sync",
                        {
                            "sessionId": args.session_id,
                            "queueSongIds": queue_song_ids,
                            "currentIndex": 0,
                            "positionMs": 0,
                        },
                        request_id=f"queue-sync-{int(time.time())}",
                    ),
                )
                continue

            command_name = raw_command
            payload = {}
            if raw_command.startswith("seek "):
                _, position_ms = raw_command.split(" ", 1)
                command_name = "seek"
                payload["positionMs"] = int(position_ms.strip())

            action_map = {
                "play": "player.play",
                "pause": "player.pause",
                "next": "player.next",
                "prev": "player.prev",
                "seek": "player.seek",
            }
            action = action_map.get(command_name)
            if action is None:
                print("unknown command")
                continue
            if not args.target_client_id:
                print("missing --target-client-id")
                continue

            send_message(
                sio,
                build_message(
                    "command",
                    action,
                    payload,
                    request_id=f"command-{int(time.time())}",
                    target_client_id=args.target_client_id,
                ),
            )
    except KeyboardInterrupt:
        print("controller stopped")
    finally:
        sio.disconnect()


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Minimal emo websocket demo client")
    parser.add_argument("mode", choices=["player", "controller"])
    parser.add_argument("--url", default="http://127.0.0.1:5000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--device-name", required=True)
    parser.add_argument("--alias")
    parser.add_argument("--session-id", default="sess-main")
    parser.add_argument("--roles", nargs="+", default=["controller"])
    parser.add_argument("--track-id", default="demo-track")
    parser.add_argument("--target-client-id")
    parser.add_argument("--queue-json")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.mode == "player" and "player" not in args.roles:
        args.roles.append("player")
    if args.mode == "controller" and "controller" not in args.roles:
        args.roles.append("controller")

    if args.mode == "player":
        run_player(args)
    else:
        run_controller(args)


if __name__ == "__main__":
    main()
