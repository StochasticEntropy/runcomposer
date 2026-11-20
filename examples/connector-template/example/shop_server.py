"""A tiny web shop to point the connector at while you try it out.

Not part of the connector: a stand-in *target*, so that the example checks
have something real to request. The checks themselves are real HTTP
requests and their verdicts are what this server answered.

    python example/shop_server.py --port 18765

Endpoints: ``/`` and ``/health``; ``/catalog`` (JSON); ``POST /cart/add``;
``/checkout``; ``/account`` (401 without a session). Start it with
``--broken checkout`` to make one endpoint answer 500.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PRODUCTS = [{"sku": "CATALOG-7", "title": "A Field Guide to Carts"}, {"sku": "CART-2", "title": "Checkout Mug"}]


def make_server(port: int = 0, *, broken: tuple[str, ...] = ()) -> ThreadingHTTPServer:
    class Shop(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return None

        def _answer(self, status: int, body: str, content_type: str = "text/plain") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _route(self) -> None:
            path = urlsplit(self.path).path
            name = path.strip("/").split("/")[0] or "home"
            if name in broken:
                return self._answer(500, "internal error")
            if self.command in ("GET", "HEAD") and path in ("/", "/health"):
                return self._answer(200, "web shop ok")
            if self.command == "GET" and path == "/catalog":
                return self._answer(200, json.dumps({"products": PRODUCTS}), "application/json")
            if self.command == "POST" and path == "/cart/add":
                return self._answer(200, "added to cart")
            if self.command == "GET" and path == "/checkout":
                return self._answer(200, "checkout: 0 items")
            if self.command == "GET" and path == "/account":
                return self._answer(401, "sign in first")
            return self._answer(404, "not found")

        do_GET = do_POST = do_HEAD = _route

    server = ThreadingHTTPServer(("127.0.0.1", port), Shop)
    server.daemon_threads = True
    return server


def start_in_background(port: int = 0, *, broken: tuple[str, ...] = ()) -> tuple[ThreadingHTTPServer, str]:
    """For tests: serve on a free port in a daemon thread; ``(server, base URL)``."""
    server = make_server(port, broken=broken)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--broken", action="append", default=[], help="an endpoint that answers 500")
    args = parser.parse_args()
    server = make_server(args.port, broken=tuple(args.broken))
    print(f"web shop listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
