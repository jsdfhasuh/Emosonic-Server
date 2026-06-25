#!/usr/bin/env python3
import os

os.environ.setdefault("EMO_SOCKETIO_ASYNC_MODE", "threading")

from supysonic.web import create_application
from supysonic.emo.ws import socketio


app = create_application()


if __name__ == '__main__':
    debug = os.environ.get("EMO_DEBUG", "").lower() in ("1", "yes", "true", "on")
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=debug,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
