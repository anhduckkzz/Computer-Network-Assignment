import socket
import threading
import logging
import protocol
import json
import sys


# logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(threadName)s | %(message)s')
logging.basicConfig(level=logging.INFO, format='%(message)s')

SERVER_IP = '0.0.0.0'
SERVER_PORT = 9999

DB_FILE = 'server_data.json'
file_index = {}
active_clients = {}
data_lock = threading.Lock()
listening_socket = None

def load_data():
    global file_index
    try:
        with open(DB_FILE, 'r') as file:
            file_index = json.load(file)
    except FileNotFoundError:
        file_index = {}
        logging.warning(f"Database file {DB_FILE} not found. Starting with empty index.")
    except json.JSONDecodeError:
        file_index = {}
        logging.error(f"Error decoding JSON from {DB_FILE}. Starting with empty index.")
        
def save_data():
    try:
        with open(DB_FILE, 'w') as file:
            json.dump(file_index, file, indent=4)
            logging.info(f"Data saved to {DB_FILE}")
    except Exception as e:
        logging.error(f"Error saving data to {DB_FILE}: {e}")
        
def handle_client(client_socket, client_address):
    thread_name = threading.current_thread().name
    client_ip = client_address[0]
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
        with data_lock:
            if client_hostname not in active_clients:
                active_clients[client_hostname] = []
            active_clients[client_hostname].append(client_info)
        protocol.send_message(client_socket, {'status': 'success', 'message': 'Hello from server!'})
        
        while True:
            message = protocol.receive_message(client_socket)
            if message is None:
                logging.warning(f"Connection closed by {client_address}")
                return
            logging.info(f"Received message from {client_address}: {message}")
            
            if message.get('action') == 'publish':
                lname = message.get('lname')
                fname = message.get('fname')
                if not lname or not fname:
                    response = {'status': 'error', 'message': 'Missing lname or fname'}
                    protocol.send_message(client_socket, response)
                    continue
                peer_info = {'hostname': client_hostname, 'ip': client_ip, 'port' : client_p2p_port, 'lname': lname}
                logging.info(f"[{thread_name}] Client {client_address} publishing file {fname}")
                with data_lock: # Bảo vệ truy cập dữ liệu chung
                    if fname not in file_index:
                        file_index[fname] = []
                    file_index[fname].append(peer_info)
                    save_data()
                response = {'status': 'success', 'message': f'File {fname} published successfully'}
                protocol.send_message(client_socket, response)
                
            elif message.get('action') == 'fetch':
                fname = message.get('fname')
                if not fname:
                    response = {'status': 'error', 'message': 'Missing fname'}
                    protocol.send_message(client_socket, response)
                logging.info(f"[{thread_name}] Client {client_address} fetching file list")
                peer_list = []
                with data_lock:
                    peer_list = file_index.get(fname, [])
                response = {'status': 'success', 'peer_list': peer_list}
                protocol.send_message(client_socket, response)
                logging.info(f"Sent peer list for file {fname} to {client_address}")
                
            else:
                response = {'status': 'error', 'message': 'Invalid action'}
                protocol.send_message(client_socket, response)
            
    except Exception as e:
        logging.error(f"[{thread_name}] Error handling client {client_address}: {e}")
    finally:
        # Đảm bảo client_hostname và client_p2p_port đã được lấy thành công
        if client_hostname and client_p2p_port:
            with data_lock:
                # 1. Xóa client khỏi active_clients
                client_info_to_remove = {'ip': client_ip, 'port': client_p2p_port}
                if client_hostname in active_clients:
                    # Lọc bỏ instance client cụ thể đã ngắt kết nối
                    active_clients[client_hostname] = [
                        info for info in active_clients[client_hostname]
                        if not (info['ip'] == client_info_to_remove['ip'] and info['port'] == client_info_to_remove['port'])
                    ]
                    if not active_clients[client_hostname]: # Nếu không còn instance nào cho hostname này
                        del active_clients[client_hostname]
                        logging.info(f"[{thread_name}] Hostname {client_hostname} removed from active clients as all instances disconnected.")

                # 2. Deregister các file đã publish bởi instance client cụ thể này
                deregistered_count = 0
                files_to_remove_completely = []
                # Lặp qua một bản sao của file_index.items() để cho phép sửa đổi trong quá trình lặp
                for fname, peer_list in list(file_index.items()):
                    original_len = len(peer_list)
                    updated_peer_list = [
                        peer for peer in peer_list
                        if not (peer['ip'] == client_ip and peer['port'] == client_p2p_port)
                    ]
                    if len(updated_peer_list) < original_len: # Nếu có peer nào đó đã bị xóa
                        deregistered_count += (original_len - len(updated_peer_list))
                        if updated_peer_list:
                            file_index[fname] = updated_peer_list
                        else:
                            files_to_remove_completely.append(fname) # Đánh dấu để xóa hoàn toàn entry file
                
                for fname in files_to_remove_completely:
                    del file_index[fname]
                
                if deregistered_count > 0:
                    logging.info(f"[{thread_name}] Deregistered {deregistered_count} file entries for disconnected client {client_address}.")
                    save_data() # Lưu thay đổi vào file_index sau khi deregister
        client_socket.close()
        logging.info(f"Closed connection with {client_address}")

def server_listen(alternative_socket):
    while True:
        try:
            client_connection, client_address = alternative_socket.accept()
            logging.info(f"Accepted connection from {client_address}! Calling handler...")
            
            client_handler = threading.Thread(target=handle_client, args=(client_connection, client_address))
            client_handler.daemon = True
            client_handler.start()
        except socket.error as e:
            logging.error(f"Socket error: {e}")
            break
        except Exception as e:
            logging.error(f"An error occurred: {e}")

def run_server():
    load_data() # Tải dữ liệu từ file khi khởi động server
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # Cho phép tái sử dụng địa chỉ
    
    try:
        listening_socket.bind((SERVER_IP, SERVER_PORT))
        listening_socket.listen(5) # Lắng nghe kết nối với độ dài hàng đợi là 5
        
        threading.current_thread().name = "Main Thread"
        logging.info(f"Server listening on IP: {SERVER_IP} - Port: {SERVER_PORT}")
        
        alternative_thread = threading.Thread(target=server_listen, args=(listening_socket,))
        alternative_thread.daemon = True
        alternative_thread.start()
        
        while True:
            cmd_line = input("Enter discover <hostname>/ ping <hostname>/ exit: ")
            if not cmd_line:
                continue
            cmd_parts = cmd_line.split()
            action = cmd_parts[0].lower()
            
            if action == 'discover' and len(cmd_parts) == 2:
                hostname = cmd_parts[1]
                logging.info(f"Discovering file of client: {hostname}")
                found_files = []
                with data_lock:
                    for fname, peer_list in file_index.items():
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
                online_list = []
                with data_lock:
                    online_list = active_clients.get(hostname, [])
                if online_list:
                    logging.info(f"PING: Client {hostname} is ONLINE")
                    logging.info(f"There are {len(online_list)} client(s) online:")
                    for client in online_list:
                        logging.info(f"- {client['ip']}: {client['port']}")
                else:
                    logging.info(f"PING: Client {hostname} is OFFLINE")
            elif action == 'exit':
                logging.info("Shutting down server.")
                break
            else:
                logging.warning(f"Invalid command: {cmd_line}")

    except KeyboardInterrupt:
        logging.info("Server interrupted (Ctrl+C).")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        save_data()  # Lưu dữ liệu vào file khi tắt server
        if listening_socket:
            listening_socket.close()
        logging.info("Server socket closed.")
        sys.exit(0)
        
if __name__ == "__main__":
    run_server()