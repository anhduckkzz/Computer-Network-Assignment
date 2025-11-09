"""Executable-specific server extensions (shared files listing)."""

from __future__ import annotations

import logging
import threading
from typing import Tuple

import protocol
import server as base_server


class ExecutableServer(base_server.Server):
    """Server variant with support for listing all shared files."""

    def handle_client(self, client_socket, client_address: Tuple[str, int]) -> None:  # type: ignore[override]
        thread_name = threading.current_thread().name  # pragma: no cover
        client_ip, _ = client_address
        client_hostname = None
        client_p2p_port = None
        logging.info("[%s] Handling client %s", thread_name, client_address)

        try:
            intro_message = protocol.receive_message(client_socket)
            if not intro_message or intro_message.get("action") != "hello":
                logging.warning("Must receive valid 'hello' message from %s first", client_address)
                protocol.send_message(client_socket, {"status": "error", "message": "Expected hello message"})
                return

            client_hostname = intro_message.get("hostname")
            client_p2p_port = intro_message.get("p2p_port")
            logging.info(
                "Client %s identified as %s with P2P port %s", client_address, client_hostname, client_p2p_port
            )
            client_info = {"ip": client_ip, "port": client_p2p_port}
            with self.data_lock:
                if client_hostname not in self.active_clients:
                    self.active_clients[client_hostname] = []
                self.active_clients[client_hostname].append(client_info)
            protocol.send_message(client_socket, {"status": "success", "message": "Hello from server!"})

            while not self.shutdown_event.is_set():
                message = protocol.receive_message(client_socket)
                if message is None:
                    logging.warning("Connection closed by %s", client_address)
                    break

                action = message.get("action")
                if action != "ping":
                    logging.info("Received message from %s: %s", client_address, message)

                if action == "publish":
                    self._handle_publish_action(message, client_address, client_hostname, client_p2p_port, client_ip, client_socket, thread_name)
                elif action == "fetch":
                    self._handle_fetch_action(message, client_address, client_socket, thread_name)
                elif action == "list_shared_files":
                    self._handle_list_shared_files(client_socket)
                elif action == "ping":
                    response = {"status": "success", "message": "pong"}
                    protocol.send_message(client_socket, response)
                else:
                    response = {"status": "error", "message": "Invalid action"}
                    protocol.send_message(client_socket, response)

        except Exception as exc:  # pragma: no cover - defensive logging
            if not self.shutdown_event.is_set():
                logging.error("[%s] Error handling client %s: %s", thread_name, client_address, exc)
        finally:
            if client_hostname and client_p2p_port:
                with self.data_lock:
                    client_info_to_remove = {"ip": client_ip, "port": client_p2p_port}
                    if client_hostname in self.active_clients:
                        self.active_clients[client_hostname] = [
                            info
                            for info in self.active_clients[client_hostname]
                            if not (info["ip"] == client_info_to_remove["ip"] and info["port"] == client_info_to_remove["port"])
                        ]
                        if not self.active_clients[client_hostname]:
                            del self.active_clients[client_hostname]
                            logging.info(
                                "[%s] Hostname %s removed from active clients as all instances disconnected.",
                                thread_name,
                                client_hostname,
                            )
                removed = self.db.delete_entries_for_peer(client_hostname, client_ip, client_p2p_port)
                deregistered_count = sum(removed.values())
                if deregistered_count > 0:
                    logging.info(
                        "[%s] Deregistered %d file entries for disconnected client %s: %s.",
                        thread_name,
                        deregistered_count,
                        client_address,
                        list(removed.keys()),
                    )
            client_socket.close()
            logging.info("Closed connection with %s", client_address)

    def _handle_publish_action(self, message, client_address, client_hostname, client_p2p_port, client_ip, client_socket, thread_name):
        lname = message.get("lname")
        fname = message.get("fname")
        allow_overwrite = bool(message.get("allow_overwrite"))
        if not lname or not fname:
            response = {"status": "error", "message": "Missing lname or fname"}
        else:
            peer_info = {
                "hostname": client_hostname,
                "ip": client_ip,
                "port": client_p2p_port,
                "lname": lname,
                "file_size": message.get("file_size"),
                "last_modified": message.get("last_modified"),
                "fname": fname,
            }
            existing_entry = None
            if client_hostname and client_ip and client_p2p_port:
                existing_entry = self.db.get_entry(fname, client_hostname, client_ip, client_p2p_port)

            if existing_entry:
                same_file_path = existing_entry.get("lname") == lname
                metadata_matches = (
                    same_file_path
                    and existing_entry.get("file_size") == peer_info["file_size"]
                    and existing_entry.get("last_modified") == peer_info["last_modified"]
                )
                if metadata_matches:
                    logging.info(
                        "[%s] Client %s attempted to republish %s with unchanged metadata",
                        thread_name,
                        client_address,
                        fname,
                    )
                    response = {
                        "status": "unchanged",
                        "message": f"File {fname} is already up to date for this client.",
                    }
                elif not same_file_path and not allow_overwrite:
                    logging.info(
                        "[%s] Client %s publish conflict on alias %s (existing path %s, new path %s)",
                        thread_name,
                        client_address,
                        fname,
                        existing_entry.get("lname"),
                        lname,
                    )
                    response = {
                        "status": "conflict",
                        "message": f"Alias '{fname}' is already published for this client.",
                        "existing_lname": existing_entry.get("lname"),
                    }
                else:
                    result = self.db.register_file(peer_info)
                    logging.info(
                        "[%s] Client %s overwrote alias %s with path %s",
                        thread_name,
                        client_address,
                        fname,
                        lname,
                    )
                    response = {
                        "status": "updated",
                        "message": f"File {fname} metadata updated.",
                        "result": result,
                    }
            else:
                result = self.db.register_file(peer_info)
                logging.info("[%s] Client %s publishing new file %s", thread_name, client_address, fname)
                response = {"status": "created", "message": f"File {fname} published successfully", "result": result}
        protocol.send_message(client_socket, response)

    def _handle_fetch_action(self, message, client_address, client_socket, thread_name):
        fname = message.get("fname")
        if not fname:
            response = {"status": "error", "message": "Missing fname"}
        else:
            logging.info("[%s] Client %s fetching file list", thread_name, client_address)
            peer_list = self.db.list_peers_for_file(fname)
            response = {"status": "success", "peer_list": peer_list}
            logging.info("Sent peer list for file %s to %s", fname, client_address)
        protocol.send_message(client_socket, response)

    def _handle_list_shared_files(self, client_socket):
        try:
            files = self.db.list_all_shared_files()
        except Exception as exc:
            logging.error("Failed to load shared files: %s", exc)
            response = {"status": "error", "message": "Unable to load shared files"}
        else:
            response = {"status": "success", "files": files}
        protocol.send_message(client_socket, response)


def install_server_patch() -> None:
    """Replace base server.Server with the executable-specific subclass."""
    base_server.Server = ExecutableServer  # type: ignore[assignment]
