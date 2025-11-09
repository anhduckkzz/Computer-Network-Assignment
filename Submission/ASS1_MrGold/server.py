import logging
import socket
import sys
import threading
from typing import List, Optional

import protocol

from database import DEFAULT_DB_URL, Database

# logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(threadName)s | %(message)s')
logging.basicConfig(level=logging.INFO, format='%(message)s')


class Server:
    def __init__(self, ip: str, port: int, db_url: Optional[str] = None):
        self.ip = ip
        self.port = port
        self.db = Database(dsn=db_url or DEFAULT_DB_URL)
        self.active_clients: dict[str, List[dict[str, object]]] = {}
        self.data_lock = threading.Lock()
        self.listening_socket: Optional[socket.socket] = None
        self.shutdown_event = threading.Event()

    def load_data(self) -> None:
        """Warm up the database connection and log existing metadata."""
        try:
            entries = self.db.fetch_all_entries()
            logging.info("Loaded %d entries from PostgreSQL metadata store.", len(entries))
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.error("Unable to load existing metadata: %s", exc)

    def list_files_by_hostname(self, hostname: str) -> List[str]:
        return self.db.list_files_by_hostname(hostname)

    def handle_client(self, client_socket: socket.socket, client_address: tuple[str, int]) -> None:
        thread_name = threading.current_thread().name
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
                    # Chỉ log nếu KHÔNG PHẢI là "ping"
                    logging.info("Received message from %s: %s", client_address, message)

                if action == "publish":
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

                elif action == "fetch":
                    fname = message.get("fname")
                    if not fname:
                        response = {"status": "error", "message": "Missing fname"}
                    else:
                        logging.info("[%s] Client %s fetching file list", thread_name, client_address)
                        peer_list = self.db.list_peers_for_file(fname)
                        response = {"status": "success", "peer_list": peer_list}
                        logging.info("Sent peer list for file %s to %s", fname, client_address)
                    protocol.send_message(client_socket, response)

                elif action == "ping":
                    # Chỉ cần trả lời "pong" để Client biết Server còn sống
                    response = {"status": "success", "message": "pong"}
                    protocol.send_message(client_socket, response)

                else:
                    response = {"status": "error", "message": "Invalid action"}
                    protocol.send_message(client_socket, response)

        except Exception as exc:
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

    def _listen_for_clients(self) -> None:
        if not self.listening_socket:
            raise RuntimeError("Server socket not initialised.")
        self.listening_socket.settimeout(1.0)
        while not self.shutdown_event.is_set():
            try:
                client_connection, client_address = self.listening_socket.accept()
                logging.info("Accepted connection from %s! Calling handler...", client_address)
                client_handler = threading.Thread(
                    target=self.handle_client,
                    args=(client_connection, client_address),
                    name=f"ClientHandler-{client_address}",
                )
                client_handler.daemon = True
                client_handler.start()
            except socket.timeout:
                continue
            except socket.error as exc:
                if not self.shutdown_event.is_set():
                    logging.error("Socket error in listener: %s", exc)
                break
            except Exception as exc:
                if not self.shutdown_event.is_set():
                    logging.error("An error occurred in listener: %s", exc)
                break

    def _handle_admin_commands(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                cmd_line = input("Enter discover <hostname>/ ping <hostname>/ exit: ")
                if not cmd_line:
                    continue
                cmd_parts = cmd_line.split()
                action = cmd_parts[0].lower()

                if action == "discover" and len(cmd_parts) == 2:
                    hostname = cmd_parts[1]
                    logging.info("Discovering file of client: %s", hostname)
                    found_files = self.db.list_files_by_hostname(hostname)
                    if found_files:
                        logging.info("Files published by %s: %s", hostname, found_files)
                    else:
                        logging.info("No files found for client %s", hostname)

                elif action == "ping" and len(cmd_parts) == 2:
                    hostname = cmd_parts[1]
                    with self.data_lock:
                        online_list = list(self.active_clients.get(hostname, []))
                    if online_list:
                        logging.info("PING: Client %s is ONLINE", hostname)
                        logging.info("There are %d client(s) online:", len(online_list))
                        for client in online_list:
                            logging.info("- %s: %s", client["ip"], client["port"])
                    else:
                        logging.info("PING: Client %s is OFFLINE", hostname)
                elif action == "exit":
                    logging.info("Shutting down server.")
                    self.shutdown()
                    break
                else:
                    logging.warning("Invalid command: %s", cmd_line)
            except (EOFError, KeyboardInterrupt):
                logging.info("Server interrupted. Shutting down.")
                self.shutdown()
                break

    def run(self) -> None:
        self.load_data()  # Tải dữ liệu từ database khi khởi động server
        self.listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Cho phép tái sử dụng địa chỉ

        try:
            self.listening_socket.bind((self.ip, self.port))
            self.listening_socket.listen(5)  # Lắng nghe kết nối với độ dài hàng đợi là 5
            threading.current_thread().name = "Main Thread"
            logging.info("Server listening on IP: %s - Port: %s", self.ip, self.port)

            listener_thread = threading.Thread(target=self._listen_for_clients, name="ClientListenerThread")
            listener_thread.daemon = True
            listener_thread.start()

            self._handle_admin_commands()
            listener_thread.join()

        except KeyboardInterrupt:
            logging.info("Server interrupted (Ctrl+C).")
            self.shutdown()
        except Exception as exc:
            logging.error("An error occurred: %s", exc)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if not self.shutdown_event.is_set():
            self.shutdown_event.set()
            logging.info("Shutdown signal sent.")
            if self.listening_socket:
                self.listening_socket.close()
                self.listening_socket = None
            self.db.close()
            logging.info("Server socket closed.")


def _run_cli_server() -> None:
    server_instance = Server(ip="0.0.0.0", port=9999, db_url=DEFAULT_DB_URL)
    server_instance.run()


if __name__ == "__main__":
    sys.modules.setdefault("server", sys.modules[__name__])
    try:
        from server_ui import main as server_ui_main
    except Exception as exc:
        logging.error(f"Unable to launch server UI: {exc}")
        logging.info("Falling back to CLI mode.")
        _run_cli_server()
    else:
        server_ui_main(auto_start=True)
    sys.exit(0)
