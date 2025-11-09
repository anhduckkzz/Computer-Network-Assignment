import logging
import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import client
import protocol


PASTEL_BG = "#ffe6f2"
PASTEL_ACCENT = "#ffccd5"
PASTEL_BUTTON = "#ffb3c6"
LOG_BG = "#fff0f5"
LOG_FG = "#333333"


class QueueHandler(logging.Handler):
    """Forward log records into a queue so the Tkinter UI can display them."""

    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.log_queue.put((message, record.levelno))


class ClientController:
    """Manage the lifecycle of client.Client for the UI."""

    def __init__(self):
        self.client = None
        self.p2p_thread = None
        self.connected = False
        self._lock = threading.Lock()
        self._socket_lock = threading.Lock()
        self.pinger_thread = None            # Sẽ giữ "máy dò tim"
        self.needs_reconnect = threading.Event() # "Cờ hiệu" báo reconnect
        self._last_connect_args = None       # Lưu lại thông tin connect

    def connect(self, server_ip, server_port, p2p_port, client_name=None):
        with self._lock:
            if self.connected:
                raise RuntimeError("Client already connected.")
        self._last_connect_args = (server_ip, server_port, p2p_port, client_name)

        cli = client.Client(
            server_ip=server_ip,
            server_port=server_port,
            p2p_port=p2p_port,
            hostname=client_name,
        )
        cli.stop_event.clear()

        p2p_thread = threading.Thread(
            target=cli._start_p2p_listener,
            name="P2PListenerThread",
            daemon=True,
        )
        p2p_thread.start()

        time.sleep(0.2)

        try:
            logging.info("Connecting to server at %s:%s as %s...", server_ip, server_port, cli.hostname)
            cli.server_socket.connect((server_ip, server_port))
            with self._socket_lock:
                intro_message = {"action": "hello", "hostname": cli.hostname, "p2p_port": cli.p2p_port}
                protocol.send_message(cli.server_socket, intro_message)
                response = protocol.receive_message(cli.server_socket)
            logging.info("Received response from server: %s", response)
        except Exception as exc:
            cli.stop_event.set()
            try:
                cli.server_socket.close()
            except Exception:
                pass
            if p2p_thread.is_alive():
                p2p_thread.join(timeout=1.0)
            raise RuntimeError(f"Failed to connect: {exc}") from exc
        else:
            with self._lock:
                self.client = cli
                self.p2p_thread = p2p_thread
                self.connected = True
                                

                # Khởi động "máy dò tim"

            self.pinger_thread = threading.Thread(

                target=self._pinger_loop,

                daemon=True,

                name="PingerThread"

            )

            self.pinger_thread.start()

            
            logging.info("Client connected and ready as %s.", cli.hostname)
            return response

    def disconnect(self):
        with self._lock:
            if not self.connected or not self.client:
                return
            cli = self.client
            p2p_thread = self.p2p_thread
            self.connected = False
            self.client = None
            self.p2p_thread = None
            self.needs_reconnect.clear()

        cli.stop_event.set()
        with self._socket_lock:
            try:
                cli.server_socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                cli.server_socket.close()
            except Exception:
                pass

        if p2p_thread and p2p_thread.is_alive():
            p2p_thread.join(timeout=2.0)

        logging.info("Client disconnected.")

    def publish(self, local_path, alias, allow_overwrite=False):
        if not self.connected or not self.client:
            raise RuntimeError("Client is not connected.")
        with self._socket_lock:
            return self.client._do_publish(local_path, alias, allow_overwrite=allow_overwrite)

    def fetch_peer_list(self, fname):
        if not self.connected or not self.client:
            raise RuntimeError("Client is not connected.")

        with self._socket_lock:
            fetch_message = {"action": "fetch", "fname": fname}
            if not protocol.send_message(self.client.server_socket, fetch_message):
                raise RuntimeError("Failed to send fetch message.")

            response = protocol.receive_message(self.client.server_socket)
            if not response or response.get("status") != "success":
                raise RuntimeError(f"Fetch failed or no response: {response}")

        peer_list = response.get("peer_list", [])
        return peer_list

    def list_shared_files(self):
        if not self.connected or not self.client:
            raise RuntimeError("Client is not connected.")

        with self._socket_lock:
            request = {"action": "list_shared_files"}
            if not protocol.send_message(self.client.server_socket, request):
                raise RuntimeError("Failed to send shared files request.")

            response = protocol.receive_message(self.client.server_socket)
            if not response or response.get("status") != "success":
                raise RuntimeError(f"Shared files request failed: {response}")

        files = response.get("files", [])
        if not isinstance(files, list):
            raise RuntimeError("Invalid shared files response from server.")
        return files

    def download_from_peer(self, peer_info, destination_path):
        if not self.connected or not self.client:
            raise RuntimeError("Client is not connected.")
        self.client._download_from_peer(peer_info, destination_path)

    def _pinger_loop(self):
        logging.info("Heartbeat thread started.")
        my_client = self.client
        if not my_client: return

        while not my_client.stop_event.is_set():
            # Chờ 5 giây. Nếu stop_event được set, nó sẽ thoát sớm
            if my_client.stop_event.wait(timeout=5.0):
                break # Bị ra lệnh dừng (do disconnect)

            try:
                # Bắt đầu "ping"
                with self._socket_lock:
                    # Kiểm tra lại, lỡ user vừa bấm disconnect
                    with self._lock:
                        if not self.connected: break
                    
                    if not protocol.send_message(my_client.server_socket, {'action': 'ping'}):
                        raise RuntimeError("Failed to send ping")
                    response = protocol.receive_message(my_client.server_socket)
                    if not response:
                        raise RuntimeError("Server closed connection")
                logging.debug("Heartbeat ping successful.")
            
            except Exception as e:
                # LỖI! Server sập rồi!
                if my_client.stop_event.is_set(): break # Lỗi do chủ động tắt
                
                logging.warning(f"Heartbeat failed: {e}. Server is down. Triggering auto-reconnect.")
                
                # 1. "Giương cờ" báo cho UI
                self.needs_reconnect.set() 
                
                # 2. Tự dọn dẹp (giống disconnect)
                with self._lock:
                    if not self.connected or not self.client:
                        break # Đã bị dọn dẹp rồi
                    # Đóng socket cũ
                    try:
                        self.client.server_socket.shutdown(socket.SHUT_RDWR)
                    except Exception: pass
                    try:
                        self.client.server_socket.close()
                    except Exception: pass
                    # Cập nhật trạng thái
                    self.connected = False 
                
                break # Dừng "máy dò tim" này lại
        
        logging.info("Heartbeat thread stopped.")
class ClientUI:
    def __init__(
        self,
        root,
        *,
        default_server_ip="127.0.0.1",
        default_server_port=9999,
        default_p2p_port=None,
        default_client_name=None,
        auto_connect=False,
    ):
        self.root = root
        self.controller = ClientController()
        self.log_queue = queue.Queue()
        self.log_handler = QueueHandler(self.log_queue)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.INFO)

        p2p_default_value = str(default_p2p_port) if default_p2p_port is not None else "10000"

        self.server_ip_var = tk.StringVar(value=str(default_server_ip))
        self.server_port_var = tk.StringVar(value=str(default_server_port))
        self.p2p_port_var = tk.StringVar(value=p2p_default_value)
        default_name = default_client_name or socket.gethostname()
        self.client_name_var = tk.StringVar(value=default_name)
        self.local_file_var = tk.StringVar()
        self.alias_var = tk.StringVar()
        self.fetch_name_var = tk.StringVar()
        self.shared_files_cache = []
        self.shared_files_after_id = None
        self._shared_refresh_inflight = False

        self._build_ui()
        self._poll_log_queue()
        self.root.after(5000, self._poll_reconnect)

        if auto_connect:
            self.root.after(100, self.connect_to_server)

    def _build_ui(self):
        self.root.title("P2P Client")
        self.root.configure(bg=PASTEL_BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        header = tk.Label(
            self.root,
            text="Client Controller",
            bg=PASTEL_BG,
            fg="#5f0f40",
            font=("Segoe UI", 16, "bold"),
        )
        header.pack(padx=10, pady=(10, 5))

        connection_frame = tk.Frame(self.root, bg=PASTEL_BG, bd=1, relief=tk.GROOVE)
        connection_frame.pack(fill=tk.X, padx=10, pady=5)

        self._add_labeled_entry(connection_frame, "Server IP:", self.server_ip_var, row=0)
        self._add_labeled_entry(connection_frame, "Server Port:", self.server_port_var, row=1)
        self._add_labeled_entry(connection_frame, "My P2P Port:", self.p2p_port_var, row=2)
        self._add_labeled_entry(connection_frame, "Client Name:", self.client_name_var, row=3)

        btn_frame = tk.Frame(connection_frame, bg=PASTEL_BG)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=5, sticky="ew")
        btn_frame.columnconfigure((0, 1), weight=1)

        self.connect_button = tk.Button(
            btn_frame,
            text="Connect",
            command=self.connect_to_server,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        self.connect_button.grid(row=0, column=0, padx=5, sticky="ew")

        self.disconnect_button = tk.Button(
            btn_frame,
            text="Disconnect",
            command=self.disconnect_from_server,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
            state=tk.DISABLED,
        )
        self.disconnect_button.grid(row=0, column=1, padx=5, sticky="ew")

        publish_frame = tk.LabelFrame(self.root, text="Publish File", bg=PASTEL_BG, fg="#5f0f40")
        publish_frame.pack(fill=tk.X, padx=10, pady=5)

        file_entry = tk.Entry(publish_frame, textvariable=self.local_file_var, bg="#fff7fb", relief=tk.FLAT)
        file_entry.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        browse_button = tk.Button(
            publish_frame,
            text="Browse",
            command=self.browse_file,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        browse_button.grid(row=0, column=1, padx=5, pady=5)

        publish_frame.columnconfigure(0, weight=1)
        publish_frame.columnconfigure(1, weight=1)

        self._add_labeled_entry(publish_frame, "Alias:", self.alias_var, row=1)

        self.publish_button = tk.Button(
            publish_frame,
            text="Publish",
            command=self.publish_file,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        self.publish_button.grid(row=1, column=2, padx=5, pady=5)

        fetch_frame = tk.LabelFrame(self.root, text="Fetch File", bg=PASTEL_BG, fg="#5f0f40")
        fetch_frame.pack(fill=tk.X, padx=10, pady=5)

        self._add_labeled_entry(fetch_frame, "File Name:", self.fetch_name_var, row=0)

        self.fetch_button = tk.Button(
            fetch_frame,
            text="Fetch",
            command=self.fetch_file,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        self.fetch_button.grid(row=0, column=2, padx=5, pady=5)

        shared_frame = tk.LabelFrame(self.root, text="Shared Files", bg=PASTEL_BG, fg="#5f0f40")
        shared_frame.pack(fill=tk.BOTH, padx=10, pady=5)

        list_container = tk.Frame(shared_frame, bg=PASTEL_BG)
        list_container.grid(row=0, column=0, rowspan=2, padx=5, pady=5, sticky="nsew")

        self.shared_files_listbox = tk.Listbox(
            list_container,
            height=8,
            bg="#fff7fb",
            relief=tk.FLAT,
            activestyle=tk.NONE,
            exportselection=False,
        )
        self.shared_files_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        shared_scrollbar = tk.Scrollbar(list_container, command=self.shared_files_listbox.yview)
        shared_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.shared_files_listbox.configure(yscrollcommand=shared_scrollbar.set)

        shared_frame.columnconfigure(0, weight=1)
        shared_frame.columnconfigure(1, weight=0)
        shared_frame.rowconfigure(0, weight=1)
        shared_frame.rowconfigure(1, weight=1)

        self.refresh_shared_button = tk.Button(
            shared_frame,
            text="Refresh Files",
            command=self.refresh_shared_files,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
            state=tk.DISABLED,
        )
        self.refresh_shared_button.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        self.shared_fetch_button = tk.Button(
            shared_frame,
            text="Fetch Selected",
            command=self.fetch_selected_shared_file,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
            state=tk.DISABLED,
        )
        self.shared_fetch_button.grid(row=1, column=1, padx=5, pady=(0, 5), sticky="ew")

        self.shared_files_listbox.bind("<<ListboxSelect>>", self._on_shared_selection_change)
        self.shared_files_listbox.bind("<Double-Button-1>", self._on_shared_file_activated)
        self.shared_files_listbox.bind("<Return>", self._on_shared_file_activated)

        log_header = tk.Frame(self.root, bg=PASTEL_BG)
        log_header.pack(fill=tk.X, padx=10, pady=(10, 0))

        log_label = tk.Label(
            log_header,
            text="Client Log",
            bg=PASTEL_BG,
            fg="#5f0f40",
            font=("Segoe UI", 12, "bold"),
        )
        log_label.pack(side=tk.LEFT)

        clear_log_button = tk.Button(
            log_header,
            text="Clear Log",
            command=self.clear_log,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        clear_log_button.pack(side=tk.RIGHT, padx=5)

        log_frame = tk.Frame(self.root, bg=PASTEL_BG)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

        self.log_text = tk.Text(
            log_frame,
            height=18,
            width=70,
            bg=LOG_BG,
            fg=LOG_FG,
            wrap=tk.WORD,
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._configure_log_tags()

        scrollbar = tk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _add_labeled_entry(self, parent, label_text, text_var, row):
        label = tk.Label(
            parent,
            text=label_text,
            bg=PASTEL_BG,
            fg="#5f0f40",
            font=("Segoe UI", 10),
        )
        label.grid(row=row, column=0, padx=5, pady=5, sticky="e")

        entry = tk.Entry(
            parent,
            textvariable=text_var,
            bg="#fff7fb",
            relief=tk.FLAT,
        )
        entry.grid(row=row, column=1, padx=5, pady=5, sticky="ew")

        parent.columnconfigure(1, weight=1)

    def connect_to_server(self):
        server_ip = self.server_ip_var.get().strip()
        server_port_value = self.server_port_var.get().strip()
        p2p_port_value = self.p2p_port_var.get().strip()
        client_name_value = self.client_name_var.get().strip()

        if not server_ip or not server_port_value or not p2p_port_value:
            messagebox.showerror("Invalid input", "Server IP, server port, and P2P port are required.")
            return

        try:
            server_port = int(server_port_value)
            p2p_port = int(p2p_port_value)
        except ValueError:
            messagebox.showerror("Invalid input", "Ports must be integers.")
            return

        self.connect_button.config(state=tk.DISABLED)
        self.disconnect_button.config(state=tk.DISABLED)

        threading.Thread(
            target=self._connect_task,
            args=(server_ip, server_port, p2p_port, client_name_value or None),
            daemon=True,
        ).start()

    def _connect_task(self, server_ip, server_port, p2p_port, client_name):
        try:
            response = self.controller.connect(server_ip, server_port, p2p_port, client_name)
        except Exception as exc:
            is_reconnecting = self.controller.needs_reconnect.is_set()
            
            if is_reconnecting:
                # Đây là lỗi DỰ KIẾN, chỉ cần log INFO "nhẹ nhàng"
                logging.info("Auto-reconnect: Server is still down.")
            else:
                # Đây là lỗi THẬT (lần đầu connect), log ERROR
                logging.error("Connection failed: %s", exc)
            self.root.after(0, lambda: self._on_connect_failed(str(exc)))
        else:
            self.root.after(0, lambda: self._on_connected(client_name, response))

    def _poll_reconnect(self):
        # 1. Hẹn giờ 5s nữa chạy lại
        self.root.after(5000, self._poll_reconnect)

        # 2. Kiểm tra cờ hiệu VÀ trạng thái
        if self.controller.needs_reconnect.is_set() and not self.controller.connected:
            # 3. Cập nhật UI sang trạng thái "Đang reconnect"
            if self.connect_button['state'] == tk.NORMAL:
                logging.info("Auto-reconnect poller: Server is down, attempting to reconnect...")
                # Tắt nút Connect, BẬT nút Disconnect (để user Cancel)
                self.connect_button.config(state=tk.DISABLED)
                self.disconnect_button.config(state=tk.NORMAL)
            
            # 4. Lấy thông tin connect cũ
            if not self.controller._last_connect_args:
                logging.error("Auto-reconnect: No connection args saved.")
                self.controller.needs_reconnect.clear() # Ngừng cố
                return
            
            (server_ip, server_port, 
            p2p_port, client_name) = self.controller._last_connect_args
            
            # 5. Bắt đầu "gọi lại" (dùng lại hàm _connect_task)
            threading.Thread(
                target=self._connect_task,
                args=(server_ip, server_port, p2p_port, client_name),
                daemon=True,
            ).start()

    def _on_connected(self, client_name, response):
        self.disconnect_button.config(state=tk.NORMAL)
        self.refresh_shared_button.config(state=tk.NORMAL)
        self.shared_fetch_button.config(state=tk.DISABLED)
        self._start_shared_files_poll()
        display_name = client_name or socket.gethostname()
        logging.info("Client UI is connected to the server as %s.", display_name)
        # Kết nối thành công, "Hạ cờ"
        self.controller.needs_reconnect.clear()
        
    def _on_connect_failed(self, message):
        if self.controller.needs_reconnect.is_set():
            # Nếu đang auto-reconnect, ta KHÔNG làm gì cả
            # Cứ để _poll_reconnect 5s sau chạy lại
            # Lỗi đã được log ra console rồi (lần trước ta sửa)
            pass
        else:
            # Nếu là lỗi "thật" (lần đầu connect do user bấm)
            # Bật lại nút Connect, Tắt nút Disconnect
            self.connect_button.config(state=tk.NORMAL)
            self.disconnect_button.config(state=tk.DISABLED)
            # Và hiện popup lỗi
            messagebox.showerror("Connection error", message)

    def disconnect_from_server(self):
        self.controller.needs_reconnect.clear()
        self.controller.disconnect()
        self.connect_button.config(state=tk.NORMAL)
        self.disconnect_button.config(state=tk.DISABLED)
        self.refresh_shared_button.config(state=tk.DISABLED)
        self.shared_fetch_button.config(state=tk.DISABLED)
        self._clear_shared_files()

    def refresh_shared_files(self):
        self._request_shared_files_refresh(manual=True)

    def _request_shared_files_refresh(self, manual=False):
        if not self.controller.connected:
            if manual:
                messagebox.showinfo("Shared files", "Connect to the server to load shared files.")
            return
        if self._shared_refresh_inflight:
            return
        self._shared_refresh_inflight = True
        if manual:
            self.refresh_shared_button.config(state=tk.DISABLED)
        threading.Thread(
            target=self._refresh_shared_files_task,
            args=(manual,),
            daemon=True,
        ).start()

    def _refresh_shared_files_task(self, manual):
        try:
            files = self.controller.list_shared_files()
        except Exception as exc:
            logging.error("Failed to refresh shared files: %s", exc)
            self.root.after(0, lambda: self._on_shared_files_failed(str(exc), manual))
            return
        self.root.after(0, lambda: self._update_shared_files(files, manual))

    def _on_shared_files_failed(self, message, manual):
        self._shared_refresh_inflight = False
        if manual:
            self.refresh_shared_button.config(state=tk.NORMAL)
            messagebox.showerror("Shared files", message)

    def _update_shared_files(self, files, _manual):
        self.shared_files_cache = list(files)
        self.shared_files_listbox.delete(0, tk.END)
        for entry in self.shared_files_cache:
            fname = entry.get("fname") or "Unknown file"
            peer_raw = entry.get("peer_count")
            peer_count = None
            if peer_raw is not None:
                try:
                    peer_count = int(peer_raw)
                except (TypeError, ValueError):
                    peer_count = None
            peer_display = ""
            if peer_count is not None:
                label = "peer" if peer_count == 1 else "peers"
                peer_display = f" - {peer_count} {label}"
            size_value = entry.get("file_size")
            size_display = self._format_file_size(size_value)
            self.shared_files_listbox.insert(tk.END, f"{fname}{peer_display} ({size_display})")
        if self.controller.connected:
            self.refresh_shared_button.config(state=tk.NORMAL)
        else:
            self.refresh_shared_button.config(state=tk.DISABLED)
        self._shared_refresh_inflight = False
        self._on_shared_selection_change()

    def _start_shared_files_poll(self):
        self._stop_shared_files_poll()
        self._request_shared_files_refresh(manual=False)
        self._schedule_shared_files_poll()

    def _schedule_shared_files_poll(self, delay_ms=5000):
        if self.controller.connected:
            self.shared_files_after_id = self.root.after(delay_ms, self._poll_shared_files)

    def _poll_shared_files(self):
        self.shared_files_after_id = None
        if not self.controller.connected:
            self._stop_shared_files_poll()
            return
        self._request_shared_files_refresh(manual=False)
        self._schedule_shared_files_poll()

    def _stop_shared_files_poll(self):
        if self.shared_files_after_id is not None:
            self.root.after_cancel(self.shared_files_after_id)
            self.shared_files_after_id = None

    def _clear_shared_files(self):
        self.shared_files_cache = []
        self.shared_files_listbox.delete(0, tk.END)
        self._shared_refresh_inflight = False
        self._on_shared_selection_change()

    def _get_shared_entry(self, index):
        if 0 <= index < len(self.shared_files_cache):
            return self.shared_files_cache[index]
        return None

    def _on_shared_selection_change(self, event=None):
        if not self.controller.connected:
            self.shared_fetch_button.config(state=tk.DISABLED)
            return
        selection = self.shared_files_listbox.curselection()
        state = tk.NORMAL if selection else tk.DISABLED
        self.shared_fetch_button.config(state=state)

    def _on_shared_file_activated(self, event=None):
        if self.shared_fetch_button.cget("state") == tk.NORMAL:
            self.fetch_selected_shared_file()

    def fetch_selected_shared_file(self):
        selection = self.shared_files_listbox.curselection()
        if not selection:
            messagebox.showinfo("Shared files", "Select a file to fetch.")
            return
        entry = self._get_shared_entry(selection[0])
        if not entry:
            messagebox.showerror("Shared files", "Selection is not available anymore.")
            return
        fname = entry.get("fname")
        if not fname:
            messagebox.showerror("Shared files", "Selected entry is missing a logical name.")
            return
        self.fetch_name_var.set(fname)
        self.fetch_file()

    def browse_file(self):
        file_path = filedialog.askopenfilename(title="Select file to publish")
        if file_path:
            self.local_file_var.set(file_path)
            if not self.alias_var.get():
                self.alias_var.set(os.path.basename(file_path))

    def publish_file(self):
        local_path = self.local_file_var.get().strip()
        alias = self.alias_var.get().strip()

        if not local_path or not alias:
            messagebox.showinfo("Publish", "Please select a file and provide an alias.")
            return

        if not os.path.exists(local_path):
            messagebox.showerror("Publish error", "Selected file does not exist.")
            return

        source_ext = os.path.splitext(local_path)[1]
        if source_ext:
            alias_root = os.path.splitext(alias)[0]
            alias = f"{alias_root}{source_ext}"
            self.alias_var.set(alias)

        threading.Thread(
            target=self._publish_task,
            args=(local_path, alias, False),
            daemon=True,
        ).start()

    def _publish_task(self, local_path, alias, allow_overwrite):
        try:
            response = self.controller.publish(local_path, alias, allow_overwrite=allow_overwrite)
        except Exception as exc:
            logging.error("Publish failed: %s", exc)
            self.root.after(0, lambda: messagebox.showerror("Publish error", str(exc)))
            return
        logging.info(
            "Publish response received for %s -> alias '%s': %s",
            local_path,
            alias,
            response,
        )
        self.root.after(
            0,
            lambda: self._handle_publish_response(local_path, alias, response, allow_overwrite),
        )

    def _handle_publish_response(self, local_path, alias, response, allow_overwrite):
        status = (response or {}).get("status")
        message = response.get("message") if isinstance(response, dict) else None
        if not isinstance(response, dict):
            messagebox.showerror("Publish error", "Unexpected response from server.")
            return

        if status == "conflict" and not allow_overwrite:
            existing_path = response.get("existing_lname") or "unknown location"
            prompt = (
                "Alias '{alias}' is already published for this client.\n\n"
                "Existing path: {existing}\n"
                "New path: {new}\n\n"
                "Do you want to overwrite the previous file entry?"
            ).format(alias=alias, existing=existing_path, new=local_path)
            overwrite = messagebox.askyesno("Overwrite alias?", prompt)
            if overwrite:
                logging.info("User confirmed overwrite for alias '%s'.", alias)
                threading.Thread(
                    target=self._publish_task,
                    args=(local_path, alias, True),
                    daemon=True,
                ).start()
            else:
                logging.info("User declined to overwrite alias '%s'.", alias)
                messagebox.showinfo("Publish", f"Publish cancelled for alias '{alias}'.")
            return

        if status in ("created", "updated"):
            title = "Publish" if status == "created" else "Publish Updated"
            messagebox.showinfo(title, message or f"Alias '{alias}' published successfully.")
            return

        if status == "unchanged":
            messagebox.showinfo("Publish", message or f"Alias '{alias}' is already up to date.")
            return

        if status == "error":
            messagebox.showerror("Publish error", message or f"Failed to publish '{alias}'.")
            return

        messagebox.showinfo("Publish", message or f"Alias '{alias}' publish result: {status}")

    def fetch_file(self):
        fname = self.fetch_name_var.get().strip()
        if not fname:
            messagebox.showinfo("Fetch", "Please enter the file name to fetch.")
            return

        self.fetch_button.config(state=tk.DISABLED)

        threading.Thread(
            target=self._fetch_peer_list_task,
            args=(fname,),
            daemon=True,
        ).start()

    def _fetch_peer_list_task(self, fname):
        try:
            peer_list = self.controller.fetch_peer_list(fname)
        except Exception as exc:
            logging.error("Fetch failed: %s", exc)
            self.root.after(0, lambda: self._on_fetch_peer_list_failed(str(exc)))
            return

        self.root.after(0, lambda: self._handle_peer_list(fname, peer_list))

    def _on_fetch_peer_list_failed(self, message):
        self.fetch_button.config(state=tk.NORMAL)
        messagebox.showerror("Fetch error", message)

    def _handle_peer_list(self, fname, peer_list):
        if not peer_list:
            logging.info("File '%s' not found on any peer.", fname)
            messagebox.showinfo("Fetch", f"File '{fname}' not found on any peer.")
            self.fetch_button.config(state=tk.NORMAL)
            return

        selected_indices = self._show_peer_selection(fname, peer_list)
        if selected_indices is None:
            logging.info("Fetch cancelled; no peer selected.")
            self.fetch_button.config(state=tk.NORMAL)
            return

        if len(selected_indices) == 1:
            chosen_peer = peer_list[selected_indices[0]]
            default_name = self._get_preferred_filename(chosen_peer, fname)
            save_path = filedialog.asksaveasfilename(
                title="Save Downloaded File",
                initialfile=default_name,
                defaultextension=os.path.splitext(default_name)[1],
            )
            if not save_path:
                logging.info("Fetch cancelled; no destination selected.")
                self.fetch_button.config(state=tk.NORMAL)
                return

            if os.path.exists(save_path):
                overwrite = messagebox.askyesno("Overwrite?", f"File '{save_path}' exists. Overwrite?")
                if not overwrite:
                    logging.info("Fetch cancelled; user chose not to overwrite %s.", save_path)
                    self.fetch_button.config(state=tk.NORMAL)
                    return

            threading.Thread(
                target=self._download_task,
                args=(chosen_peer, save_path),
                daemon=True,
            ).start()
            return

        target_directory = filedialog.askdirectory(
            title="Select Destination Directory for Downloads",
            mustexist=True,
        )
        if not target_directory:
            logging.info("Fetch cancelled; no destination directory selected.")
            self.fetch_button.config(state=tk.NORMAL)
            return

        download_tasks = []
        for index in selected_indices:
            peer = peer_list[index]
            filename = self._get_preferred_filename(peer, fname)
            destination = self._unique_destination_path(target_directory, filename)
            download_tasks.append((peer, destination))

        logging.info("Starting batch download for %d peer(s).", len(download_tasks))
        threading.Thread(
            target=self._download_multiple_task,
            args=(download_tasks,),
            daemon=True,
        ).start()

    def _show_peer_selection(self, fname, peer_list):
        if len(peer_list) == 1:
            return [0]

        dialog = tk.Toplevel(self.root)
        dialog.title("Select Peer(s)")
        dialog.transient(self.root)
        dialog.grab_set()

        instruction = tk.Label(
            dialog,
            text="Choose peer(s) to download from.\n"
            "Use Ctrl/Shift for multi-select or pick an option below.",
            justify=tk.LEFT,
        )
        instruction.pack(padx=10, pady=(10, 5), anchor="w")

        listbox = tk.Listbox(dialog, selectmode=tk.MULTIPLE, width=60, height=min(10, len(peer_list)))
        listbox.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

        for idx, peer in enumerate(peer_list, start=1):
            client_label = peer.get("hostname") or peer.get("ip") or "Unknown client"
            size_label = self._format_file_size(peer.get("file_size"))
            listbox.insert(
                tk.END,
                f"{idx}. {client_label} ({size_label})",
            )

        button_frame = tk.Frame(dialog)
        button_frame.pack(padx=10, pady=(5, 10), fill=tk.X)
        button_frame.columnconfigure((0, 1, 2, 3), weight=1)

        result = {"indices": None}

        def on_select():
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Selection required", "Select at least one peer.", parent=dialog)
                return
            result["indices"] = list(selection)
            dialog.destroy()

        def on_select_all():
            result["indices"] = list(range(len(peer_list)))
            dialog.destroy()

        def on_custom():
            raw = simpledialog.askstring(
                "Custom selection",
                "Enter peer numbers separated by commas (e.g. 1,3,4):",
                parent=dialog,
            )
            if raw is None:
                return
            try:
                indices = []
                for chunk in raw.replace(" ", "").split(","):
                    if not chunk:
                        continue
                    value = int(chunk)
                    if value < 1 or value > len(peer_list):
                        raise ValueError(f"Peer number {value} is out of range.")
                    zero_based = value - 1
                    if zero_based not in indices:
                        indices.append(zero_based)
            except ValueError as exc:
                messagebox.showerror("Invalid input", str(exc), parent=dialog)
                return
            if not indices:
                messagebox.showinfo("Selection required", "Provide at least one valid peer number.", parent=dialog)
                return
            result["indices"] = indices
            dialog.destroy()

        def on_cancel():
            result["indices"] = None
            dialog.destroy()

        select_button = tk.Button(button_frame, text="Download Selected", command=on_select, bg=PASTEL_BUTTON)
        select_button.grid(row=0, column=0, padx=5, sticky="ew")

        all_button = tk.Button(button_frame, text="Download All", command=on_select_all, bg=PASTEL_BUTTON)
        all_button.grid(row=0, column=1, padx=5, sticky="ew")

        custom_button = tk.Button(button_frame, text="Custom", command=on_custom, bg=PASTEL_BUTTON)
        custom_button.grid(row=0, column=2, padx=5, sticky="ew")

        cancel_button = tk.Button(button_frame, text="Cancel", command=on_cancel, bg=PASTEL_BUTTON)
        cancel_button.grid(row=0, column=3, padx=5, sticky="ew")

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        dialog.resizable(False, False)
        dialog.focus_set()
        self.root.wait_window(dialog)
        return result["indices"]

    def _download_task(self, peer_info, save_path):
        try:
            self.controller.download_from_peer(peer_info, save_path)
        except Exception as exc:
            logging.error("Download failed: %s", exc)
            self.root.after(0, lambda: self._on_download_finished(False, str(exc), peer_info, save_path))
            return

        self.root.after(0, lambda: self._on_download_finished(True, None, peer_info, save_path))

    def _on_download_finished(self, success, error_message, peer_info, save_path):
        if success:
            ip = peer_info.get("ip") if isinstance(peer_info, dict) else "?"
            port = peer_info.get("port") if isinstance(peer_info, dict) else "?"
            logging.info(
                "Download completed from %s:%s to '%s'.",
                ip,
                port,
                save_path,
            )
            messagebox.showinfo("Fetch", f"Download completed:\n{save_path}")
        else:
            messagebox.showerror("Download error", error_message)
        self.fetch_button.config(state=tk.NORMAL)

    def _download_multiple_task(self, download_tasks):
        successes = []
        failures = []
        for peer_info, save_path in download_tasks:
            try:
                self.controller.download_from_peer(peer_info, save_path)
            except Exception as exc:
                logging.error("Download failed for %s: %s", save_path, exc)
                failures.append((peer_info, save_path, str(exc)))
            else:
                successes.append((peer_info, save_path))
        self.root.after(
            0,
            lambda: self._on_multi_download_finished(successes, failures),
        )

    def _on_multi_download_finished(self, successes, failures):
        messages = []
        if successes:
            success_lines = "\n".join(f"- {path}" for _, path in successes)
            messages.append(f"Downloaded {len(successes)} file(s):\n{success_lines}")
        if failures:
            failure_lines = "\n".join(
                f"- {os.path.basename(path)} ({err})" for _, path, err in failures
            )
            messages.append(f"Failed downloads:\n{failure_lines}")

        summary = "\n\n".join(messages) if messages else "No downloads were completed."
        messagebox.showinfo("Fetch summary", summary)
        self.fetch_button.config(state=tk.NORMAL)

    def _format_file_size(self, size_value):
        try:
            size = int(size_value)
        except (TypeError, ValueError):
            return "unknown size"
        units = ["B", "KB", "MB", "GB", "TB"]
        for unit in units:
            if size < 1024 or unit == "TB":
                if unit == "B":
                    return f"{size} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _get_preferred_filename(self, peer_info, fallback_name):
        original = peer_info.get("lname")
        if original:
            return os.path.basename(original)
        return os.path.basename(fallback_name)

    def _unique_destination_path(self, directory, filename):
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(directory, filename)
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(directory, f"{base}_{counter}{ext}")
            counter += 1
        return candidate

    def clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_log_queue(self):
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            else:
                if isinstance(item, tuple):
                    message, levelno = item
                else:
                    message, levelno = item, logging.INFO
                self._append_log(message, levelno)
        self.root.after(200, self._poll_log_queue)

    def _append_log(self, message, levelno):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n", self._get_log_tag(levelno))
        self.log_text.configure(state=tk.DISABLED)
        self.log_text.see(tk.END)

    def _configure_log_tags(self):
        self.log_text.tag_configure('DEBUG', foreground='#0277bd')
        self.log_text.tag_configure('INFO', foreground='#2e7d32')
        self.log_text.tag_configure('WARNING', foreground='#f9a825')
        self.log_text.tag_configure('ERROR', foreground='#c62828')
        self.log_text.tag_configure('DEFAULT', foreground=LOG_FG)

    def _get_log_tag(self, levelno):
        if levelno >= logging.ERROR:
            return 'ERROR'
        if levelno >= logging.WARNING:
            return 'WARNING'
        if levelno >= logging.INFO:
            return 'INFO'
        if levelno >= logging.DEBUG:
            return 'DEBUG'
        return 'DEFAULT'

    def on_close(self):
        self._stop_shared_files_poll()
        self.controller.disconnect()
        logging.getLogger().removeHandler(self.log_handler)
        self.root.destroy()


def main(
    default_server_ip="127.0.0.1",
    default_server_port=9999,
    default_p2p_port=None,
    default_client_name=None,
    auto_connect=False,
):
    root = tk.Tk()
    ClientUI(
        root,
        default_server_ip=default_server_ip,
        default_server_port=default_server_port,
        default_p2p_port=default_p2p_port,
        default_client_name=default_client_name,
        auto_connect=auto_connect,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
