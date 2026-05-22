"""In-process HTTP stub for an LSPS1 provider.

One `MockLSPServer` instance impersonates a single LSP (either Zeus or
Megalithic) for tests. Configurable per-test responses for:

  - GET  /api/v1/get_info
  - POST /api/v1/create_order
  - GET  /api/v1/get_order

Tests inject a `lsp_providers._RestLSPProvider` subclass that points at
the stub's `http://127.0.0.1:<port>` base URL, then invoke
`liquidityhelper.request_inbound_liquidity_from_lsp` etc.

Lightweight: stdlib `http.server` in a daemon thread; no aiohttp on the
test side, no Docker. Tests typically finish in <100 ms each.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockLSPServer:
    """A configurable LSPS1 HTTP stub.

    Defaults to returning a reasonable get_info + an order with
    fee_total_sat=1000 from create_order. Tests override via the
    set_*_response methods or set_*_handler for dynamic responses.
    """

    DEFAULT_GET_INFO = {
        "supported_versions": [1],
        # uris[0] is the LSP's currently-advertised peer URI (LSPS1 spec).
        # Our provider abstraction uses this to dynamically discover the
        # peer pubkey for ConnectPeer, falling back to the hardcoded URI
        # if get_info doesn't return one.
        "uris": [
            "aa" * 33 + "@mock-lsp.test:9735",
        ],
        "options": {
            "min_required_channel_confirmations": 0,
            "min_funding_confirms_within_blocks": 6,
            "supports_zero_channel_reserve": True,
            "min_onchain_payment_size_sat": 0,
            "max_channel_expiry_blocks": 13000,
            "min_initial_client_balance_sat": "0",
            "max_initial_client_balance_sat": "0",
            "min_initial_lsp_balance_sat": "100000",
            "max_initial_lsp_balance_sat": "10000000",
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
        },
    }

    @staticmethod
    def _default_create_order_response(req: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "order_id": "test-order-001",
            "lsp_balance_sat": req.get("lsp_balance_sat", "150000"),
            "client_balance_sat": "0",
            "channel_expiry_blocks": req.get("channel_expiry_blocks", 52596),
            "order_state": "CREATED",
            "announce_channel": False,
            "payment": {
                "state": "EXPECT_PAYMENT",
                "fee_total_sat": "1000",
                "order_total_sat": "1000",
                "bolt11_invoice": "lnbcdummyinvoicemockserver",
                "onchain_address": "bcrt1qmockonchainaddressforlsporderpayment",
                "min_onchain_payment_confirmations": 0,
            },
        }

    def __init__(self, *, port: Optional[int] = None) -> None:
        self.port = port if port is not None else _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        # Request log so tests can assert what was sent.
        self.requests: List[Dict[str, Any]] = []
        # Response config — either a static dict or a callable taking the
        # request JSON and returning the response dict.
        self._get_info_response: Any = dict(self.DEFAULT_GET_INFO)
        self._create_order_response: Any = self._default_create_order_response
        self._get_order_response: Any = {
            "order_id": "test-order-001",
            "order_state": "PAID",
        }
        # status-code overrides (None = OK)
        self._get_info_status: Optional[int] = None
        self._create_order_status: Optional[int] = None
        self._get_order_status: Optional[int] = None

        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -------- response config (sync, called from tests) -----------------

    def set_get_info_response(self, resp: Dict[str, Any]) -> None:
        self._get_info_response = resp

    def set_create_order_response(
        self, resp: Any
    ) -> None:
        """Either a dict (returned verbatim) or a callable
        f(req_json) -> dict for response-depends-on-input cases."""
        self._create_order_response = resp

    def set_get_order_response(self, resp: Dict[str, Any]) -> None:
        self._get_order_response = resp

    def fail_get_info(self, status: int = 500) -> None:
        self._get_info_status = status

    def fail_create_order(self, status: int = 500) -> None:
        self._create_order_status = status

    # -------- lifecycle -------------------------------------------------

    def start(self) -> None:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kw):
                pass  # silence the default access-log spam

            def _send(self, status: int, body: Any) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                outer.requests.append({"method": "GET", "path": self.path})
                # Match by suffix so we serve both Zeus (/api/v1/...)
                # and Megalithic (/api/lsps1/v1/...) path prefixes from
                # the same mock implementation.
                if path.endswith("/get_info"):
                    if outer._get_info_status:
                        self._send(outer._get_info_status,
                                   {"error": "test failure"})
                        return
                    self._send(200, outer._get_info_response)
                    return
                if path.endswith("/get_order"):
                    if outer._get_order_status:
                        self._send(outer._get_order_status,
                                   {"error": "test failure"})
                        return
                    self._send(200, outer._get_order_response)
                    return
                self._send(404, {"error": f"unknown path {path}"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    req_json = json.loads(raw.decode()) if raw else {}
                except Exception:
                    req_json = {}
                outer.requests.append({
                    "method": "POST", "path": self.path, "json": req_json,
                })
                if self.path.split("?", 1)[0].endswith("/create_order"):
                    if outer._create_order_status:
                        self._send(outer._create_order_status,
                                   {"error": "test failure"})
                        return
                    resp = outer._create_order_response
                    if callable(resp):
                        resp = resp(req_json)
                    self._send(200, resp)
                    return
                self._send(404, {"error": f"unknown path {self.path}"})

        self._httpd = HTTPServer(("127.0.0.1", self.port), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
