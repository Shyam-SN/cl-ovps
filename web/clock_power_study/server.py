#!/usr/bin/env python3
"""Serve the static CL/OVPS AES results and flow explainer locally."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parent
ADDRESS = ("127.0.0.1", 8765)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    handler = partial(QuietHandler, directory=str(WEB))
    server = ThreadingHTTPServer(ADDRESS, handler)
    print("CL/OVPS AES power study is ready at http://127.0.0.1:8765")
    print("Press Control-C to stop the local website.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWebsite stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
