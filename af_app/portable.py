"""Windows portable launcher for the bundled SangforBox service."""

from __future__ import annotations

import argparse
import logging
import logging.config
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from af_app.app import create_app
from af_app.config import Settings, default_state_directory


def installation_directory() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd().resolve()


def bundled_directory() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def prepare_directories(root: Path) -> tuple[Path, Path, Path]:
    """Prepare the per-user state directory.

    Runtime state lives outside the installation directory so the portable
    folder stays read-only in practice: it can be replaced with a new build
    without losing device profiles, review records or exports. ``root`` is kept
    for the legacy in-place layout only as a migration source.
    """
    state = default_state_directory()
    results = state / "results"
    logs = state / "logs"
    for directory in (state, results, logs):
        directory.mkdir(parents=True, exist_ok=True)
    database = state / "af.db"
    if not database.exists():
        legacy = root / "data" / "af.db"
        if legacy.is_file():
            shutil.copyfile(legacy, database)
        else:
            seed = bundled_directory() / "assets" / "knowledge.db"
            if not seed.is_file():
                raise RuntimeError("内置 AF 知识库缺失，请重新获取完整便携包。")
            temporary = state / "af.db.initializing"
            shutil.copyfile(seed, temporary)
            os.replace(temporary, database)
    return database, results, logs


def logging_config(log_file: Path) -> dict:
    formatter = {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"portable": formatter},
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "portable", "stream": "ext://sys.stdout"},
            "file": {"class": "logging.handlers.RotatingFileHandler", "formatter": "portable",
                     "filename": str(log_file), "encoding": "utf-8", "maxBytes": 5_000_000, "backupCount": 3},
        },
        "loggers": {
            "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["console", "file"], "level": "INFO"},
    }


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def open_when_ready(url: str) -> None:
    for _ in range(120):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the portable SangforBox MCP server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    host = "127.0.0.1"
    root = installation_directory()
    database, results, logs = prepare_directories(root)
    log_file = logs / "sangforbox.log"
    config = logging_config(log_file)
    logging.config.dictConfig(config)
    logger = logging.getLogger("sangforbox.portable")
    if not port_available(host, args.port):
        logger.error("端口 %s 已被占用。若 SangforBox 已启动，请直接打开 http://%s:%s/", args.port, host, args.port)
        return 2
    url = f"http://{host}:{args.port}/"
    logger.info("SangforBox 安装目录：%s", root)
    logger.info("SangforBox 数据目录：%s", database.parent)
    logger.info("导出目录：%s", results)
    logger.info("WorkBuddy MCP 地址：http://%s:%s/mcp/", host, args.port)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    settings = Settings(database, output_directory=results)
    try:
        uvicorn.run(create_app(settings), host=host, port=args.port, log_config=config, access_log=True)
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("SangforBox 启动失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
