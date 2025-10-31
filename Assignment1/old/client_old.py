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

class Client:
    def __init__(self, server_ip, server_port, p2p_port):
        self.server_ip = server_ip
        self.server_port = server_port
        self.p2p_port = p2p_port
        self.hostname = socket.gethostname()
        self.stop_event = threading.Event() # Sự kiện để dừng luồng lắng nghe P2P
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Bắt đầu luồng lắng nghe kết nối P2P
    def _start_p2p_listener(self):
        p2p_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        p2p_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            p2p_socket.bind(('', self.p2p_port))
            p2p_socket.listen(5)
            logging.info(f"P2P listener started on port {self.p2p_port}")

            p2p_socket.settimeout(1.0)  # Check stop_event 1 giây một lần

            while not self.stop_event.is_set():
                try:
                    peer_connection, peer_address = p2p_socket.accept()
                    logging.info(f"Accepted connection from {peer_address}")
                    peer_handler = threading.Thread(target=self._handle_peer, args=(peer_connection, peer_address))
                    peer_handler.daemon = True
                    peer_handler.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.stop_event.is_set():
                        break
                    logging.error(f"P2P listener error: {e}")
        except Exception as e:
            logging.error(f"P2P listener error: {e}")
        finally:
            p2p_socket.close()

    def _handle_peer(self, peer_socket, peer_address):
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

    def _do_publish(self, lname, fname):
        if not os.path.exists(lname):
            logging.error(f"File {lname} does not exist.")
            return
        publish_message = {'action': 'publish', 'lname': lname, 'fname': fname}
        if protocol.send_message(self.server_socket, publish_message):
            response = protocol.receive_message(self.server_socket)
            logging.info(f"Publish response: {response}")
        else:
            logging.error("Failed to send publish message.")

    def _download_from_peer(self, chosen_peer, fname_to_save):
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

    def _do_fetch(self, fname):
        fetch_message = {'action': 'fetch', 'fname': fname}
        if not protocol.send_message(self.server_socket, fetch_message):
            logging.error("Failed to send fetch message.")
            return

        response = protocol.receive_message(self.server_socket)
        if not response or response.get('status') != 'success':
            logging.error(f"Fetch failed or no response received: {response}")
            return

        peer_list = response.get('peer_list', [])
        if not peer_list:
            logging.info(f"File '{fname}' not found on any peer.")
            return

        logging.info(f"File {fname} is available from the following peer(s):")
        for i, peer in enumerate(peer_list):
            logging.info(f" [{i+1}] Hostname: {peer['hostname']}, IP: {peer['ip']}, Port: {peer['port']}")

        chosen_index = 0
        if len(peer_list) > 1:
            try:
                choice_str = input(f"Enter 1 number from 1 to {len(peer_list)} to choose a peer (default = 1): ")
                chosen_int = int(choice_str) if choice_str else 1
                if 1 <= chosen_int <= len(peer_list):
                    chosen_index = chosen_int - 1
                else:
                    logging.warning("Invalid choice, defaulting to 1.")
            except ValueError:
                logging.warning("Invalid input, defaulting to 1.")

        chosen_peer = peer_list[chosen_index]
        logging.info(f"Decided to download from peer: Hostname: {chosen_peer['hostname']}, IP: {chosen_peer['ip']}, Port: {chosen_peer['port']}")

        if os.path.exists(fname):
            overwrite = input(f"File '{fname}' already exists. Overwrite? (y/n): ").lower()
            if overwrite != 'y':
                logging.info("Download cancelled by user.")
                return
        self._download_from_peer(chosen_peer, fname)

    def run(self):
        threading.current_thread().name = "Main Thread"
        logging.info(f"Client starting with P2P port: {self.p2p_port}")

        p2p_thread = threading.Thread(target=self._start_p2p_listener, name="P2PListenerThread")
        p2p_thread.daemon = True
        p2p_thread.start()

        time.sleep(1)  # Đợi một chút để luồng lắng nghe P2P khởi động

        try:
            logging.info(f"Connecting to server at IP: {self.server_ip} - Port:{self.server_port}...")
            self.server_socket.connect((self.server_ip, self.server_port))
            logging.info("Connection established.")

            intro_message = {'action': 'hello', 'hostname': self.hostname, 'p2p_port': self.p2p_port}
            protocol.send_message(self.server_socket, intro_message)

            response = protocol.receive_message(self.server_socket)
            logging.info(f"Received response from server: {response}" if response else "No response from server.")

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
                        self._do_publish(lname, fname)
                    elif action == 'fetch' and len(parts) == 2:
                        fname = parts[1]
                        # logging.info(f"Fetching file: {fname}")
                        self._do_fetch(fname)
                    elif action == 'exit':
                        logging.info("Exiting client.")
                        break
                    else:
                        logging.warning(f"Invalid command: {cmd_line}")
                except Exception as e:
                    logging.error(f"Error processing command: {e}")

        except socket.error as e:
            logging.error(f"Connection failed: {e}")
        except (KeyboardInterrupt, EOFError):
            logging.info("Client interrupted.")
        except Exception as e:
            logging.error(f"An error occurred: {e}")
        finally:
            self.stop_event.set()  # Yêu cầu dừng luồng lắng nghe P2P
            self.server_socket.close()
            p2p_thread.join()
            logging.info("Connection closed.")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python client.py <my_p2p_port>")
        sys.exit(1)

    try:
        p2p_port = int(sys.argv[1])
    except ValueError:
        print("Invalid port number. Please provide a valid integer.")
        sys.exit(1)

    client = Client(server_ip='127.0.0.1', server_port=9999, p2p_port=p2p_port)
    client.run()