"""Run the add-on service."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import signal

from aiohttp import web

from .adapters import BaresipCtrlTcpAdapter
from .config import AppConfig
from .manager import CallManager
from .server import create_app

_LOGGER = logging.getLogger(__name__)
API_BIND_HOST = "127.0.0.1"


async def _run() -> None:
    config = AppConfig.from_file(Path("/data/options.json"))
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    adapter = BaresipCtrlTcpAdapter(config)
    manager = CallManager(config, adapter)
    runner = web.AppRunner(create_app(config, manager), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=API_BIND_HOST, port=config.api_port)
    await site.start()
    _LOGGER.info(
        "UniFi Talk Alarm API v1 listening on %s:%s",
        API_BIND_HOST,
        config.api_port,
    )

    try:
        await adapter.start()
    except Exception:
        _LOGGER.exception("SIP client failed to start; API remains available for diagnostics")
        await manager.set_registration_error("SIP client failed to start")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop_event.set)
    await stop_event.wait()
    await manager.shutdown()
    await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(_run())
    except ValueError as err:
        logging.basicConfig(level=logging.ERROR)
        logging.error("Invalid add-on configuration: %s", err)
        raise SystemExit(2) from err


if __name__ == "__main__":
    main()
