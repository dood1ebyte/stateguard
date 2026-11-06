"""
TEST-ONLY: a TCP proxy that delays everything by a fixed one-way time, the way a network path
does: every chunk, in both directions, and the end of the connection (a FIN) too. Order is kept.

    python delay_proxy.py --listen 127.0.0.1:PORT --upstream 127.0.0.1:PORT --delay-ms 40

On loopback a server's close reaches the client within microseconds, so a client almost never
reuses a connection the server is closing. With a delay D each way, the server's close takes D
to arrive and a request takes D to reach the server: a request sent up to 2D before the close
arrives is lost unread. ``--log`` gets one ``accept`` line per connection.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, delay: float) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[float, bytes | None]] = asyncio.Queue()

    async def deliver() -> None:
        while True:
            due, chunk = await queue.get()
            wait = due - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            if chunk is None:
                if writer.can_write_eof():
                    writer.write_eof()
                return
            writer.write(chunk)
            await writer.drain()

    sender = asyncio.create_task(deliver())
    try:
        while chunk := await reader.read(65536):
            queue.put_nowait((loop.time() + delay, chunk))
    except (ConnectionError, OSError):
        pass
    queue.put_nowait((loop.time() + delay, None))
    with contextlib.suppress(ConnectionError, OSError):
        await sender


async def serve(listen: str, upstream: str, delay: float, log: Path | None) -> None:
    up_host, _, up_port = upstream.rpartition(":")

    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        if log is not None:
            with log.open("a") as out:
                out.write("accept\n")
        try:
            up_r, up_w = await asyncio.open_connection(up_host, int(up_port))
        except OSError:
            client_w.close()
            return
        await asyncio.gather(_pipe(client_r, up_w, delay), _pipe(up_r, client_w, delay))
        for w in (client_w, up_w):
            w.close()

    host, _, port = listen.rpartition(":")
    server = await asyncio.start_server(handle, host, int(port))
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--delay-ms", type=float, default=40.0)
    parser.add_argument("--log")
    args = parser.parse_args()
    log = Path(args.log) if args.log else None
    asyncio.run(serve(args.listen, args.upstream, args.delay_ms / 1000, log))


if __name__ == "__main__":
    main()
