import socket
import threading
import logging
import protocol
import json
import sys

# logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(threadName)s | %(message)s')
logging.basicConfig(level=logging.INFO, format='%(message)s')

class Server:
    def __init__(self, ip, port, db_file):
        self.ip = ip
        self.port = port
        self.db_file = db_file
        self.file_index = {}
        self.active_clients = {}
        self.data_lock = threading.Lock()
        self.listening_socket = None
        self.shutdown_event = threading.Event()

    def load_data(self):
        try:
            with open(self.db_file, 'r') as file:
                self.file_index = json.load(file)
        except FileNotFoundError:
            self.file_index = {}
            logging.warning(f"Database file {self.db_file} not found. Starting with empty index.")
        except json.JSONDecodeError:
            self.file_index = {}
            logging.error(f"Error decoding JSON from {self.db_file}. Starting with empty index.")

    def save_data(self):
        try:
            with open(self.db_file, 'w') as file:
                json.dump(self.file_index, file, indent=4)
                logging.info(f"Data saved to {self.db_file}")
        except Exception as e:
            logging.error(f"Error saving data to {self.db_file}: {e}")

    def handle_client(self, client_socket, client_address):
        thread_name = threading.current_thread().name
        client_ip, _ = client_address
        client_hostname = None
        client_p2p_port = None
        logging.info(f"[{thread_name}] Handling client {client_address}")

        try:
            intro_message = protocol.receive_message(client_socket)
            if not intro_message or intro_message.get('action') != 'hello':
                logging.warning(f"Must receive valid 'hello' message from {client_address} first")
                protocol.send_message(client_socket, {'status': 'error', 'message': 'Expected hello message'})
                return

            client_hostname = intro_message.get('hostname')
            client_p2p_port = intro_message.get('p2p_port')
            logging.info(f"Client {client_address} identified as {client_hostname} with P2P port {client_p2p_port}")
            client_info = {'ip': client_ip, 'port': client_p2p_port}
            with self.data_lock:
                if client_hostname not in self.active_clients:
                    self.active_clients[client_hostname] = []
                self.active_clients[client_hostname].append(client_info)
            protocol.send_message(client_socket, {'status': 'success', 'message': 'Hello from server!'})

            while not self.shutdown_event.is_set():
                message = protocol.receive_message(client_socket)
                if message is None:
                    logging.warning(f"Connection closed by {client_address}")
                    break
                logging.info(f"Received message from {client_address}: {message}")

                action = message.get('action')
                if action == 'publish':
                    lname = message.get('lname')
                    fname = message.get('fname')
                    if not lname or not fname:
                        response = {'status': 'error', 'message': 'Missing lname or fname'}
                    else:
                        peer_info = {
                            'hostname': client_hostname,
                            'ip': client_ip,
                            'port': client_p2p_port,
                            'lname': lname,
                            'file_size': message.get('file_size'),
                            'last_modified': message.get('last_modified'),
                        }
                        with self.data_lock: # Bảo vệ truy cập dữ liệu chung
                            peer_list = self.file_index.setdefault(fname, [])
                            duplicate = next(
                                (
                                    existing
                                    for existing in peer_list
                                    if existing.get('hostname') == client_hostname
                                    and existing.get('ip') == client_ip
                                    and existing.get('port') == client_p2p_port
                                    and existing.get('file_size') == peer_info.get('file_size')
                                    and existing.get('last_modified') == peer_info.get('last_modified')
                                ),
                                None,
                            )
                            if duplicate:
                                logging.warning(
                                    f"[{thread_name}] File '{fname}' already published with same metadata by {client_address}"
                                )
                                response = {
                                    'status': 'exists',
                                    'message': f"File {fname} already registered with same metadata",
                                }
                            else:
                                peer_list.append(peer_info)
                                self.save_data()
                                logging.info(f"[{thread_name}] Client {client_address} publishing file {fname}")
                                response = {
                                    'status': 'success',
                                    'message': f'File {fname} published successfully',
                                }
                    protocol.send_message(client_socket, response)

                elif action == 'fetch':
                    fname = message.get('fname')
                    if not fname:
                        response = {'status': 'error', 'message': 'Missing fname'}
                    else:
                        logging.info(f"[{thread_name}] Client {client_address} fetching file list")
                        with self.data_lock:
                            peer_list = self.file_index.get(fname, [])
                        response = {'status': 'success', 'peer_list': peer_list}
                        logging.info(f"Sent peer list for file {fname} to {client_address}")
                    protocol.send_message(client_socket, response)

                else:
                    response = {'status': 'error', 'message': 'Invalid action'}
                    protocol.send_message(client_socket, response)

        except Exception as e:
            if not self.shutdown_event.is_set():
                logging.error(f"[{thread_name}] Error handling client {client_address}: {e}")
        finally:
            # Đảm bảo client_hostname và client_p2p_port đã được lấy thành công
            if client_hostname and client_p2p_port:
                with self.data_lock:
                    # 1. Xóa client khỏi active_clients
                    client_info_to_remove = {'ip': client_ip, 'port': client_p2p_port}
                    if client_hostname in self.active_clients:
                        # Lọc bỏ instance client cụ thể đã ngắt kết nối
                        self.active_clients[client_hostname] = [
                            info for info in self.active_clients[client_hostname]
                            if not (info['ip'] == client_info_to_remove['ip'] and info['port'] == client_info_to_remove['port'])
                        ]
                        if not self.active_clients[client_hostname]: # Nếu không còn instance nào cho hostname này
                            del self.active_clients[client_hostname]
                            logging.info(f"[{thread_name}] Hostname {client_hostname} removed from active clients as all instances disconnected.")

                    # 2. Deregister các file đã publish bởi instance client cụ thể này
                    deregistered_count = 0
                    files_to_remove_completely = []
                    # Lặp qua một bản sao của file_index.items() để cho phép sửa đổi trong quá trình lặp
                    for fname, peer_list in list(self.file_index.items()):
                        original_len = len(peer_list)
                        updated_peer_list = [
                            peer for peer in peer_list
                            if not (peer['ip'] == client_ip and peer['port'] == client_p2p_port)
                        ]
                        if len(updated_peer_list) < original_len: # Nếu có peer nào đã bị loại bỏ
                            deregistered_count += (original_len - len(updated_peer_list))
                            if updated_peer_list:
                                self.file_index[fname] = updated_peer_list
                            else:
                                files_to_remove_completely.append(fname) # Đánh dấu để xóa hoàn toàn entry file

                    for fname in files_to_remove_completely:
                        del self.file_index[fname]

                    if deregistered_count > 0:
                        logging.info(f"[{thread_name}] Deregistered {deregistered_count} file entries for disconnected client {client_address}.")
                        self.save_data() # Lưu thay đổi vào file_index sau khi deregister
            client_socket.close()
            logging.info(f"Closed connection with {client_address}")

    def _listen_for_clients(self):
        self.listening_socket.settimeout(1.0)
        while not self.shutdown_event.is_set():
            try:
                client_connection, client_address = self.listening_socket.accept()
                logging.info(f"Accepted connection from {client_address}! Calling handler...")
                client_handler = threading.Thread(target=self.handle_client, args=(client_connection, client_address))
                client_handler.daemon = True
                client_handler.start()
            except socket.timeout:
                continue
            except socket.error as e:
                if not self.shutdown_event.is_set():
                    logging.error(f"Socket error in listener: {e}")
                break
            except Exception as e:
                if not self.shutdown_event.is_set():
                    logging.error(f"An error occurred in listener: {e}")
                break

    def _handle_admin_commands(self):
        while not self.shutdown_event.is_set():
            try:
                cmd_line = input("Enter discover <hostname>/ ping <hostname>/ exit: ")
                if not cmd_line:
                    continue
                cmd_parts = cmd_line.split()
                action = cmd_parts[0].lower()

                if action == 'discover' and len(cmd_parts) == 2:
                    hostname = cmd_parts[1]
                    logging.info(f"Discovering file of client: {hostname}")
                    found_files = []
                    with self.data_lock:
                        for fname, peer_list in self.file_index.items():
                            for peer in peer_list:
                                if peer['hostname'] == hostname:
                                    found_files.append(fname)
                                    break
                    if found_files:
                        logging.info(f"Files published by {hostname}: {found_files}")
                    else:
                        logging.info(f"No files found for client {hostname}")

                elif action == 'ping' and len(cmd_parts) == 2:
                    hostname = cmd_parts[1]
                    with self.data_lock:
                        online_list = self.active_clients.get(hostname, [])
                    if online_list:
                        logging.info(f"PING: Client {hostname} is ONLINE")
                        logging.info(f"There are {len(online_list)} client(s) online:")
                        for client in online_list:
                            logging.info(f"- {client['ip']}: {client['port']}")
                    else:
                        logging.info(f"PING: Client {hostname} is OFFLINE")
                elif action == 'exit':
                    logging.info("Shutting down server.")
                    self.shutdown()
                    break
                else:
                    logging.warning(f"Invalid command: {cmd_line}")
            except (EOFError, KeyboardInterrupt):
                logging.info("Server interrupted. Shutting down.")
                self.shutdown()
                break

    def run(self):
        self.load_data() # Tải dữ liệu từ file khi khởi động server
        self.listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # Cho phép tái sử dụng địa chỉ

        try:
            self.listening_socket.bind((self.ip, self.port))
            self.listening_socket.listen(5) # Lắng nghe kết nối với độ dài hàng đợi là 5
            threading.current_thread().name = "Main Thread"
            logging.info(f"Server listening on IP: {self.ip} - Port: {self.port}")

            listener_thread = threading.Thread(target=self._listen_for_clients, name="ClientListenerThread")
            listener_thread.daemon = True
            listener_thread.start()

            self._handle_admin_commands()
            listener_thread.join()

        except KeyboardInterrupt:
            logging.info("Server interrupted (Ctrl+C).")
            self.shutdown()
        except Exception as e:
            logging.error(f"An error occurred: {e}")
        finally:
            self.shutdown()

    def shutdown(self):
        if not self.shutdown_event.is_set():
            self.shutdown_event.set()
            logging.info("Shutdown signal sent.")
            self.save_data()  # Lưu dữ liệu vào file khi tắt server
            if self.listening_socket:
                self.listening_socket.close()
            logging.info("Server socket closed.")


def _run_cli_server():
    server_instance = Server(ip='0.0.0.0', port=9999, db_file='server_data.json')
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
