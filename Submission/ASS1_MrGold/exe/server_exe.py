"""PyInstaller-friendly entry point for the server that relies on SQLite instead of PostgreSQL."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional


def _project_root() -> Path:
    if getattr(sys, "frozen", False):  # type: ignore[attr-defined]
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):  # type: ignore[attr-defined]
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bootstrap_paths() -> None:
    exe_dir = _exe_dir()
    project_root = _project_root()
    for path in (exe_dir, project_root):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


_bootstrap_paths()

from server_impl import ExecutableServer, install_server_patch

install_server_patch()


class _SuppressSharedListLogFilter(logging.Filter):
    """Hide noisy list_shared_files poll logs while leaving other messages intact."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not (msg.startswith("Received message from") and "'action': 'list_shared_files'" in msg)


def _configure_logging(level: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s | %(message)s")
    logging.getLogger().addFilter(_SuppressSharedListLogFilter())


def _sqlite_url_override(db_file: Optional[str]) -> Optional[str]:
    if not db_file:
        return None
    target = Path(db_file)
    if not target.is_absolute():
        target = (_exe_dir() / target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{target}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the P2P metadata server as a packaged executable.")
    parser.add_argument("--host", default="0.0.0.0", help="IP address to bind (default: %(default)s)")
    parser.add_argument("--port", type=int, default=9999, help="TCP port to bind (default: %(default)s)")
    parser.add_argument(
        "--db-file",
        help="Optional path to the SQLite metadata file. Defaults to p2p_metadata.db next to the executable.",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Skip the Tkinter UI and run the CLI server loop directly.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, ...). Default: %(default)s",
    )
    return parser.parse_args()


def _run_cli_server(host: str, port: int, db_url: Optional[str]) -> None:
    server_instance = ExecutableServer(ip=host, port=port, db_url=db_url)
    server_instance.run()


def _launch_ui(host: str, port: int, db_url: Optional[str]) -> None:
    try:
        from server_ui import main as server_ui_main
    except Exception as exc:
        logging.warning("Unable to launch server UI (%s). Falling back to CLI mode.", exc)
        _run_cli_server(host, port, db_url)
        return

    if db_url:
        # server_ui pulls DEFAULT_DB_URL from the database module, so override it via environment.
        import database

        database.DEFAULT_DB_URL = db_url  # type: ignore[attr-defined]

    logging.info("Starting server UI (auto_start=True).")
    server_ui_main(auto_start=True)


def main() -> None:
    args = _parse_args()
    _configure_logging(args.log_level)
    db_url = _sqlite_url_override(args.db_file)
    if args.no_ui:
        _run_cli_server(args.host, args.port, db_url)
    else:
        _launch_ui(args.host, args.port, db_url)


if __name__ == "__main__":
    main()
