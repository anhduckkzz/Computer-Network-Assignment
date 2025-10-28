import socket
import threading
import logging
import protocol
import sys
import shlex
import time
import os

# logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logging.basicConfig(level=logging.INFO, format='%(message)s')

TARGET_SERVER_IP = '127.0.0.1'
TARGET_SERVER_PORT = 9999

if len(sys.argv) != 2:
    print("Usage: python client.py <my_p2p_port>")
    sys.exit(1)

try:
    MY_2P2_PORT = int(sys.argv[1])
except ValueError:
    print("Invalid port number. Please provide a valid integer.")
    sys.exit(1)
    
MY_HOSTNAME = socket.gethostname()

stop_event = threading.Event() # Sự kiện để dừng luồng lắng nghe P2P

# Bắt đầu luồng lắng nghe kết nối P2P
def start_p2p_listener(p2p_port):
    p2p_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    p2p_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        p2p_socket.bind(('', p2p_port))
        p2p_socket.listen(5)
        logging.info(f"P2P listener started on port {p2p_port}")

        p2p_socket.settimeout(1.0)  # Check stop_event 1 giây một lần

        while not stop_event.is_set():
            try:
                peer_connection, peer_address = p2p_socket.accept()
                logging.info(f"Accepted connection from {peer_address}")
                peer_handler = threading.Thread(target=handle_peer, args=(peer_connection, peer_address))
                peer_handler.daemon = True
                peer_handler.start()
            except socket.timeout:
                continue
            except Exception as e:
                if stop_event.is_set():
                    break
                logging.error(f"P2P listener error: {e}")
    except Exception as e:
        logging.error(f"P2P listener error: {e}")
    finally:
        p2p_socket.close()

def handle_peer(peer_socket, peer_address):
    thread_name = threading.current_thread().name
    logging.info(f"[{thread_name}] Handling peer {peer_address}")
    try:
        message = protocol.receive_message(peer_socket) # Chờ nhận yêu cầu xin file từ peer
        if message and message.get('action') == 'get_file':
            lname = message.get('lname') # Xử lý yêu cầu xin file từ peer
            logging.info(f"[{thread_name}] Peer {peer_address} requested file {lname}")
            if not lname or not os.path.exists(lname):
                logging.warning(f"File {lname} does not exist.")
            else:
                # Gửi file cho peer
                logging.info(f"[{thread_name}] Start sending file {lname} to {peer_address}")
                with open(lname, 'rb') as file:
                    while True:
                        chunk = file.read(4096)
                        if not chunk:
                            break
                        peer_socket.sendall(chunk)
                logging.info(f"[{thread_name}] Finished sending file {lname} to {peer_address}")
        else:
            logging.warning(f"[{thread_name}] Invalid request from peer {peer_address}")
    except Exception as e:
        logging.error(f"[{thread_name}] Error handling peer {peer_address}: {e}")
    finally:
        peer_socket.close()
        logging.info(f"[{thread_name}] Closed connection with peer {peer_address}")

def do_publish(socket, lname, fname):
    if not os.path.exists(lname):
        logging.error(f"File {lname} does not exist.")
        return
    publish_message = {'action': 'publish', 'lname': lname, 'fname': fname}
    if protocol.send_message(socket, publish_message):
        response = protocol.receive_message(socket)
        logging.info(f"Publish response: {response}")
    else:
        logging.error("Failed to send publish message.")
        
def download_request_to_peer(chosen_peer, fname_to_save):
    logging.info("Starting download from peer...")
    peer_ip = chosen_peer['ip']
    peer_port = chosen_peer['port']
    lname_on_peer = chosen_peer['lname']
    
    logging.info(f"Connecting to peer at IP: {peer_ip}, Port: {peer_port}...")
    
    p2p_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    p2p_socket.settimeout(10)  # Thiết lập timeout kết nối
    
    try:
        p2p_socket.connect((peer_ip, peer_port))
        logging.info("Connected to peer.")
        request_message = {'action': 'get_file', 'lname': lname_on_peer}
        protocol.send_message(p2p_socket, request_message)
        logging.info("Request sent to peer, starting to receive file...")
        
        bytes_downloaded = 0
        with open(fname_to_save, 'wb') as file:
            while True:
                chunk = p2p_socket.recv(4096)
                if not chunk:
                    break
                file.write(chunk)
                bytes_downloaded += len(chunk)
        logging.info(f"Download completed. Total bytes downloaded: {bytes_downloaded} bytes.")
    except socket.timeout:
        logging.error(f"Error: Over 10s, Peer {peer_ip}:{peer_port} did not respond.")
    except Exception as e:
        logging.error(f"Error downloading file from peer: {e}")
    finally:
        p2p_socket.close()
        logging.info("--- End of P2P download ---")
        

def do_fetch(socket, fname):
    fetch_message = {'action': 'fetch', 'fname': fname}
    if protocol.send_message(socket, fetch_message):
        response = protocol.receive_message(socket)
        
        if response and response.get('status') == 'success':
            peer_list = response.get('peer_list', [])
            if peer_list:
                logging.info(f"File {fname} is available from the following peer(s):")
                for i, peer in enumerate(peer_list):
                    logging.info(f" [{i+1}] Hostname: {peer['hostname']}, IP: {peer['ip']}, Port: {peer['port']}")
                
                chosen_index = 0
                if len(peer_list) > 1:
                    try:
                        choice_str = input(f"Enter 1 number from 1 to {len(peer_list)} to choose a peer (default = 1): ")
                        if not choice_str:
                            chosen_int = 1
                        else:
                            chosen_int = int(choice_str)
                        
                        if 1 <= chosen_int <= len(peer_list):
                            chosen_index = chosen_int - 1
                        else:
                            logging.warning("Invalid choice, defaulting to 1.")
                            chosen_index = 0
                    except ValueError:
                        logging.warning("Invalid input, defaulting to 1.")
                        chosen_index = 0
                chosen_peer = peer_list[chosen_index]
                logging.info(f"Decided to download from peer: Hostname: {chosen_peer['hostname']}, IP: {chosen_peer['ip']}, Port: {chosen_peer['port']}")
                
                if os.path.exists(fname):
                    overwrite = input(f"File '{fname}' already exists. Overwrite? (y/n): ").lower()
                    if overwrite != 'y':
                        logging.info("Download cancelled by user.")
                        return
                download_request_to_peer(chosen_peer, fname)
        else:
            logging.error(f"Fetch failed or no response received: {response}")
    else:
        logging.error("Failed to send fetch message.")
            

def run_client(p2p_port):
    threading.current_thread().name = "Main Thread"
    logging.info(f"Client starting with P2P port: {p2p_port}")
    
    p2p_thread = threading.Thread(target=start_p2p_listener, args=(p2p_port,))
    p2p_thread.daemon = True
    p2p_thread.start()
    
    time.sleep(1)  # Đợi một chút để luồng lắng nghe P2P khởi động
    
    client_to_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        logging.info(f"Connecting to server at IP: {TARGET_SERVER_IP} - Port:{TARGET_SERVER_PORT}...")
        client_to_server_socket.connect((TARGET_SERVER_IP, TARGET_SERVER_PORT))
        logging.info("Connection established.")
        
        intro_message = {'action': 'hello', 'hostname': MY_HOSTNAME, 'p2p_port': p2p_port}
        protocol.send_message(client_to_server_socket, intro_message)

        response = protocol.receive_message(client_to_server_socket)
        if response:
            logging.info(f"Received response from server: {response}")
        else:
            logging.warning("No response received from server.")
        
        while True:
            cmd_line = input(f"Enter publish <lname> <fname>/ fetch <fname>/ exit: ")
            if not cmd_line:
                continue
            try:
                parts = shlex.split(cmd_line)
                action = parts[0].lower()
                if action == 'publish' and len(parts) == 3:
                    lname = parts[1]
                    fname = parts[2]
                    # logging.info(f"Publishing file: {fname} with logical name: {lname}")
                    do_publish(client_to_server_socket, lname, fname)

                elif action == 'fetch' and len(parts) == 2:
                    fname = parts[1]
                    # logging.info(f"Fetching file: {fname}")
                    do_fetch(client_to_server_socket, fname)
                    
                elif action == 'exit':
                    logging.info("Exiting client.")
                    break
                else:
                    logging.warning(f"Invalid command: {cmd_line}")
            except Exception as e:
                logging.error(f"Error processing command: {e}")
    
    except socket.error as e:
        logging.error(f"Connection failed: {e}")
    except KeyboardInterrupt:
        logging.info("Client interrupted.")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        stop_event.set()  # Yêu cầu dừng luồng lắng nghe P2P
        client_to_server_socket.close()
        p2p_thread.join()
        logging.info("Connection closed.")

if __name__ == "__main__":
    run_client(MY_2P2_PORT)