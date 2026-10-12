import argparse
import asyncio
import gzip
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

import websockets

URL = "wss://stream.bybit.com/v5/public/linear"
PING_EVERY_S = 20
SUB_CHUNK = 10
FLUSH_EVERY_S = 5

log = logging.getLogger("recorder")


class HourlyWriter:
    def __init__(self, outdir):
        self.outdir = outdir
        self.fh = None
        self.hour = None
        self.last_flush = time.monotonic()
        os.makedirs(outdir, exist_ok=True)

    def write(self, recv_ns, payload):
        hour = datetime.fromtimestamp(recv_ns / 1e9, timezone.utc).strftime("%Y%m%d_%H")
        if hour != self.hour:
            self.close()
            path = os.path.join(self.outdir, f"bybit_linear_{hour}.jsonl.gz")
            self.fh = gzip.open(path, "ab", compresslevel=3)
            self.hour = hour
            log.info("writing to %s", path)
        self.fh.write(f"{recv_ns}\t{payload}\n".encode())
        now = time.monotonic()
        if now - self.last_flush >= FLUSH_EVERY_S:
            self.fh.flush()
            self.last_flush = now

    def close(self):
        if self.fh is not None:
            self.fh.flush()
            self.fh.close()
            self.fh = None


async def pinger(ws):
    while True:
        await asyncio.sleep(PING_EVERY_S)
        await ws.send('{"op":"ping"}')


async def stats(counter):
    while True:
        await asyncio.sleep(60)
        log.info("msgs last 60s: %d", counter["n"])
        counter["n"] = 0


async def record(symbols, depth, outdir):
    writer = HourlyWriter(outdir)
    topics = []
    for s in symbols:
        topics.append(f"orderbook.{depth}.{s}")
        topics.append(f"publicTrade.{s}")

    counter = {"n": 0}
    stats_task = asyncio.create_task(stats(counter))
    backoff = 1
    try:
        while True:
            try:
                async with websockets.connect(URL, ping_interval=None, max_size=None) as ws:
                    writer.write(time.time_ns(), '{"_meta":"connected"}')
                    log.info("connected, subscribing to %d topics", len(topics))
                    for i in range(0, len(topics), SUB_CHUNK):
                        await ws.send(json.dumps({"op": "subscribe", "args": topics[i:i + SUB_CHUNK]}))
                    backoff = 1
                    ping_task = asyncio.create_task(pinger(ws))
                    try:
                        async for msg in ws:
                            recv_ns = time.time_ns()
                            if isinstance(msg, bytes):
                                msg = msg.decode()
                            writer.write(recv_ns, msg)
                            counter["n"] += 1
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("connection error: %r", e)
            writer.write(time.time_ns(), '{"_meta":"disconnected"}')
            log.info("reconnecting in %ds", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    finally:
        stats_task.cancel()
        writer.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--depth", type=int, default=50)
    ap.add_argument("--out", default="./data")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def runner():
        task = asyncio.create_task(record(args.symbols, args.depth, args.out))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except NotImplementedError:
                pass
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(runner())


if __name__ == "__main__":
    main()