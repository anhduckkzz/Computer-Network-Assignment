"""Client entry point tailored for PyInstaller builds with auto-incremented identities."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Tuple

AUTO_PORT_START = 1111
AUTO_PORT_STEP = 1111
STATE_FILE_NAME = "client_launch_state.json"


class _SuppressSharedFilesRefreshFilter(logging.Filter):
    """Hide the noisy shared-files refresh error while keeping the request alive."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not msg.startswith("Failed to refresh shared files:")


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):  # type: ignore[attr-defined]
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _project_root() -> Path:
    if getattr(sys, "frozen", False):  # type: ignore[attr-defined]
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def _bootstrap_paths() -> None:
    exe_dir = _exe_dir()
    project_root = _project_root()
    for path in (exe_dir, project_root):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


_bootstrap_paths()


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(message)s",
    )
    logging.getLogger().addFilter(_SuppressSharedFilesRefreshFilter())


def _state_file() -> Path:
    return _exe_dir() / STATE_FILE_NAME


def _load_next_index(state_path: Path) -> int:
    if not state_path.exists():
        return 1
    try:
        data = json.loads(state_path.read_text())
        return int(data.get("next_index", 1))
    except Exception:
        logging.warning("State file %s is corrupt. Resetting auto-increment sequence.", state_path)
        return 1


def _store_next_index(state_path: Path, next_index: int) -> None:
    payload = json.dumps({"next_index": next_index}, indent=2)
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(payload)
    tmp_path.replace(state_path)


def _index_to_port(index: int) -> int:
    return AUTO_PORT_START + (index - 1) * AUTO_PORT_STEP


def _index_to_name(index: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    value = index
    chars = []
    while value > 0:
        value, remainder = divmod(value - 1, len(alphabet))
        chars.append(alphabet[remainder])
    return "".join(reversed(chars))


def _next_identity(
    override_port: Optional[int],
    override_name: Optional[str],
    reset: bool,
) -> Tuple[int, Optional[str]]:
    state_path = _state_file()
    if reset and state_path.exists():
        state_path.unlink()

    needs_auto = override_port is None or override_name is None
    index = None
    if needs_auto:
        index = _load_next_index(state_path)
        _store_next_index(state_path, index + 1)
        logging.info(
            "Auto-selected client slot #%s -> port=%s, name=%s",
            index,
            _index_to_port(index),
            _index_to_name(index),
        )

    if override_port is None:
        if index is None:
            raise RuntimeError("Internal error: auto index missing for port generation.")
        port = _index_to_port(index)
    else:
        port = override_port

    if override_name is None:
        if index is None:
            raise RuntimeError("Internal error: auto index missing for name generation.")
        name = _index_to_name(index)
    else:
        name = override_name

    return port, name


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch the packaged client with automatic port/name assignment."
    )
    parser.add_argument("--server-ip", default="127.0.0.1", help="Server IP (default: %(default)s)")
    parser.add_argument("--server-port", type=int, default=9999, help="Server port (default: %(default)s)")
    parser.add_argument("--p2p-port", type=int, help="Override the auto-generated P2P port.")
    parser.add_argument("--client-name", help="Override the auto-generated client name.")
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Reset the auto-increment state before launching.",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Skip the Tkinter UI and run the CLI client loop.",
    )
    parser.add_argument(
        "--auto-connect",
        action="store_true",
        help="Auto-connect once the UI appears (default False).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, ...). Default: %(default)s",
    )
    return parser.parse_args()


def _launch_ui(server_ip: str, server_port: int, p2p_port: int, client_name: Optional[str], auto_connect: bool) -> None:
    try:
        from client_ui import main as client_ui_main
    except Exception as exc:
        logging.warning("Unable to launch client UI (%s). Falling back to CLI mode.", exc)
        _run_cli_client(server_ip, server_port, p2p_port, client_name)
        return

    logging.info(
        "Starting client UI with defaults server=%s:%s, p2p_port=%s, client=%s",
        server_ip,
        server_port,
        p2p_port,
        client_name or "<auto>",
    )
    client_ui_main(
        default_server_ip=server_ip,
        default_server_port=server_port,
        default_p2p_port=p2p_port,
        default_client_name=client_name,
        auto_connect=auto_connect,
    )


def _run_cli_client(server_ip: str, server_port: int, p2p_port: int, client_name: Optional[str]) -> None:
    from client import Client

    logging.info("Launching CLI client against %s:%s.", server_ip, server_port)
    client_instance = Client(server_ip=server_ip, server_port=server_port, p2p_port=p2p_port, hostname=client_name)
    client_instance.run()


def main() -> None:
    args = _parse_args()
    _configure_logging(args.log_level)
    p2p_port, client_name = _next_identity(args.p2p_port, args.client_name, args.reset_state)
    if args.cli:
        _run_cli_client(args.server_ip, args.server_port, p2p_port, client_name)
    else:
        _launch_ui(args.server_ip, args.server_port, p2p_port, client_name, args.auto_connect)


if __name__ == "__main__":
    main()
