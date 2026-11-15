# This file is part of Supysonic.
# Distributed under terms of the GNU AGPLv3 license.

from datetime import datetime

from geventwebsocket.handler import WebSocketHandler


class EmosonicWebSocketHandler(WebSocketHandler):
    def format_request(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = (self._orig_status or self.status or "000").split()[0]
        length = self.response_length or "-"
        if self.time_finish:
            duration = f"{(self.time_finish - self.time_start):.6f}s"
        else:
            duration = "-"

        client_address = (
            self.client_address[0]
            if isinstance(self.client_address, tuple)
            else self.client_address
        ) or "-"
        method = self.command or "-"
        path = self.path or "-"
        if path.startswith("/emo/ws"):
            category = "SOCKET"
        elif path.startswith("/rest/stream"):
            category = "STREAM"
        elif path.startswith("/rest/"):
            category = "REST"
        else:
            category = "HTTP"
        return (
            f"[{now}] [ACCESS:{category}] {client_address} {method} {path} "
            f"status={status} bytes={length} duration={duration}"
        )

    def log_request(self):
        self.server.log.write(self.format_request() + "\n")
