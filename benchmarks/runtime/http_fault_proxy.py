"""
TEST-ONLY: an HTTP/1.1 proxy that injects one labelled fault into the first
MCP ``tools/call`` of a chosen tool, between a client (or StateGuard's HTTP
relay) and a Streamable HTTP MCP server. Never for production traffic.

    python benchmarks/runtime/http_fault_proxy.py --listen 127.0.0.1:9400 \\
        --upstream 127.0.0.1:9301 --mode lost_response --tool add_note \\
        --fault-file /tmp/fault.fired --log /tmp/proxy.log

Modes (the fault fires once per ``--fault-file``; every other request passes):

``none``             pass everything
``kill_before``      close the connection without forwarding (never executed)
``http_503_before``  answer 503 without forwarding (never executed)
``hang_before``      hold the connection, never forward, never answer
``lost_response``    forward, read the whole answer, close without sending it
``http_502_after``   forward, read the whole answer, answer 502 instead
``hang_after``       forward, read the whole answer, then never answer
``cut_stream``       forward; relay the event stream up to (not including)
                     the event carrying the answer, then close mid-stream
                     (a JSON answer is lost whole, as ``lost_response``)

Each request runs on its own connection (``Connection: close`` both ways), so
a fault ends exactly one exchange. ``--log`` gets one line per request:
``<method> <tool or -> <fault or pass>``; ``call <tool>`` lines count what
reached the server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

MODES = (
    "none",
    "kill_before",
    "http_503_before",
    "hang_before",
    "lost_response",
    "http_502_after",
    "hang_after",
    "cut_stream",
)


class Proxy:
    def __init__(self, args: argparse.Namespace) -> None:
        host, _, port = args.upstream.rpartition(":")
        self.upstream = (host, int(port))
        self.mode: str = args.mode
        self.tool: str = args.tool
        self.fault_file = Path(args.fault_file) if args.fault_file else None
        self.log_file = Path(args.log) if args.log else None

    def log(self, line: str) -> None:
        if self.log_file is not None:
            with self.log_file.open("a") as out:
                out.write(line + "\n")

    def fires(self, message: Any) -> bool:
        if self.mode == "none" or self.fault_file is None or self.fault_file.exists():
            return False
        params = message.get("params") if isinstance(message, dict) else None
        if not (
            isinstance(params, dict)
            and message.get("method") == "tools/call"
            and params.get("name") == self.tool
        ):
            return False
        self.fault_file.write_text(self.mode)
        return True

    async def handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            await self._handle(client_r, client_w)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            client_w.close()

    async def _handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        head = await client_r.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        request_line, header_lines = lines[0], [h for h in lines[1:] if h]
        headers = {
            h.split(":", 1)[0].strip().lower(): h.split(":", 1)[1].strip() for h in header_lines
        }
        length = int(headers.get("content-length", "0"))
        body = await client_r.readexactly(length) if length else b""
        message: Any = None
        if body:
            try:
                message = json.loads(body)
            except ValueError:
                message = None
        method = message.get("method") if isinstance(message, dict) else None
        params = message.get("params") if isinstance(message, dict) else None
        tool = params.get("name") if isinstance(params, dict) else None
        fault = self.mode if request_line.startswith("POST") and self.fires(message) else None
        self.log(f"{method or request_line.split(' ')[0]} {tool or '-'} {fault or 'pass'}")
        if fault == "kill_before":
            client_w.transport.abort()
            return
        if fault == "http_503_before":
            client_w.write(
                b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await client_w.drain()
            return
        if fault == "hang_before":
            await client_r.read()  # until the client gives up
            return
        if method == "tools/call":
            self.log(f"call {tool}")
        kept = [h for h in header_lines if h.split(":", 1)[0].strip().lower() != "connection"]
        upstream_head = "\r\n".join([request_line, *kept, "Connection: close", "", ""]).encode()
        up_r, up_w = await asyncio.open_connection(*self.upstream)
        try:
            up_w.write(upstream_head + body)
            await up_w.drain()
            if fault in ("lost_response", "http_502_after", "hang_after"):
                await up_r.read()  # the server's whole answer, discarded
                if fault == "lost_response":
                    client_w.transport.abort()
                elif fault == "http_502_after":
                    client_w.write(
                        b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    await client_w.drain()
                else:
                    await client_r.read()
                return
            if fault == "cut_stream":
                await self._cut(up_r, client_w, message)
                return
            while chunk := await up_r.read(65536):
                client_w.write(chunk)
                await client_w.drain()
        finally:
            up_w.close()

    async def _cut(
        self, up_r: asyncio.StreamReader, client_w: asyncio.StreamWriter, request: Any
    ) -> None:
        """Relay an event stream up to the event that answers *request*, then cut it."""
        head = await up_r.readuntil(b"\r\n\r\n")
        if b"text/event-stream" not in head.lower() or b"chunked" not in head.lower():
            await up_r.read()
            client_w.transport.abort()  # a JSON answer: lost whole
            return
        client_w.write(head)
        await client_w.drain()
        answer_id = request.get("id") if isinstance(request, dict) else None
        text = ""
        while True:
            size_line = await up_r.readuntil(b"\r\n")
            size = int(size_line.strip().split(b";")[0], 16)
            if size == 0:
                break
            chunk = await up_r.readexactly(size)
            await up_r.readexactly(2)
            text += chunk.decode()
            # Events end at a blank line; servers write CRLF or LF line ends.
            while (end := _EVENT_END.search(text)) is not None:
                event, text = text[: end.end()], text[end.end() :]
                if _answers(event, answer_id):
                    client_w.transport.abort()  # mid-stream: no answer, no end of stream
                    return
                data = event.encode()
                client_w.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                await client_w.drain()
        client_w.transport.abort()


_EVENT_END = re.compile(r"\r?\n\r?\n")


def _answers(event: str, answer_id: Any) -> bool:
    for line in event.splitlines():
        if line.startswith("data:"):
            try:
                message = json.loads(line[5:].strip())
            except ValueError:
                return False
            return (
                isinstance(message, dict)
                and message.get("id") == answer_id
                and ("result" in message or "error" in message)
            )
    return False


async def serve(args: argparse.Namespace) -> None:
    proxy = Proxy(args)
    host, _, port = args.listen.rpartition(":")
    server = await asyncio.start_server(proxy.handle, host, int(port))
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen", required=True)
    parser.add_argument("--upstream", required=True, help="host:port of the MCP server")
    parser.add_argument("--mode", choices=MODES, default="none")
    parser.add_argument("--tool", default="")
    parser.add_argument("--fault-file")
    parser.add_argument("--log")
    args = parser.parse_args(argv)
    asyncio.run(serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
