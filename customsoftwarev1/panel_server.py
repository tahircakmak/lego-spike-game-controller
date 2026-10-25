"""
A tiny web server for the control panel, on this computer only (127.0.0.1).

    GET  /                the control panel (panel.html)
    GET  /api/...         JSON from app.py
    POST /api/...         JSON in, JSON out

It runs in the same asyncio loop as the hub and the engine, so a request never races
with a reading. Standard library only.

Other web pages can't use it: requests must be addressed to 127.0.0.1 / localhost, and
POSTs need an "X-Panel" header, which a page on another site can't send here.
"""

import asyncio
import json
from pathlib import Path

PANEL = Path(__file__).parent / "panel.html"
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 500: "Internal Server Error"}


class PanelServer:
    def __init__(self, handle, port: int = 8765):
        """handle: async (method, path, body) -> (status, payload) for every /api/ request."""
        self.handle = handle
        self.port = port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    async def start(self) -> None:
        try:
            self.server = await asyncio.start_server(self._client, "127.0.0.1", self.port)
        except OSError:
            raise RuntimeError(f"Port {self.port} is in use (is the program already running?). "
                               "Close the other one or use --panel-port.") from None

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readline()
            method, path, _ = request.decode("latin-1").split(" ", 2)
            headers = {}
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", 0))
            body = await reader.readexactly(length) if length else b""
            path = path.split("?", 1)[0]
            host = headers.get("host", "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost") or (method == "POST" and "x-panel" not in headers):
                await self._send(writer, 403, b"forbidden", "text/plain")
            elif method == "GET" and path in ("/", "/index.html"):
                await self._send(writer, 200, PANEL.read_bytes(), "text/html; charset=utf-8")
            elif path.startswith("/api/"):
                try:
                    data = json.loads(body) if body else {}
                    status, payload = await self.handle(method, path[5:], data)
                except (ValueError, KeyError, TypeError) as e:
                    status, payload = 400, {"error": str(e)}
                except RuntimeError as e:
                    status, payload = 500, {"error": str(e)}
                await self._send(writer, status, json.dumps(payload).encode(), "application/json")
            else:
                await self._send(writer, 404, b"not found", "text/plain")
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _send(writer, status: int, body: bytes, content_type: str) -> None:
        head = (f"HTTP/1.1 {status} {REASONS.get(status, '')}\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
        writer.write(head.encode() + body)
        await writer.drain()
