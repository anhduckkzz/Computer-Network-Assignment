import logging
import queue
import socket
import threading
import tkinter as tk
from tkinter import messagebox

import server


PASTEL_BG = "#ffe6f2"
PASTEL_ACCENT = "#ffccd5"
PASTEL_BUTTON = "#ffb3c6"
LOG_BG = "#fff0f5"
LOG_FG = "#333333"


class QueueHandler(logging.Handler):
    """Send log records to a queue so the Tkinter UI can display them."""

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


class ServerController:
    """Thin wrapper around server.Server to control lifecycle from the UI."""

    def __init__(self):
        self.server = None
        self.listener_thread = None
        self.running = False

    def start(self, ip, port, db_file):
        if self.running:
            raise RuntimeError("Server already running.")

        srv = server.Server(ip=ip, port=port, db_file=db_file)
        srv.load_data()

        listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listening_socket.bind((ip, port))
        listening_socket.listen(5)
        listening_socket.settimeout(1.0)

        srv.listening_socket = listening_socket
        self.server = srv

        self.listener_thread = threading.Thread(
            target=self.server._listen_for_clients,
            name="ClientListenerThread",
            daemon=True,
        )
        self.listener_thread.start()

        self.running = True
        logging.info("Server started and listening for clients.")

    def stop(self):
        if not self.running or not self.server:
            return

        self.server.shutdown()
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2.0)

        self.server = None
        self.listener_thread = None
        self.running = False
        logging.info("Server stopped.")

    def discover(self, hostname):
        if not self.running or not self.server:
            raise RuntimeError("Server is not running.")

        found_files = []
        with self.server.data_lock:
            for fname, peer_list in self.server.file_index.items():
                if any(peer.get("hostname") == hostname for peer in peer_list):
                    found_files.append(fname)
        return found_files

    def ping(self, hostname):
        if not self.running or not self.server:
            raise RuntimeError("Server is not running.")

        with self.server.data_lock:
            clients = list(self.server.active_clients.get(hostname, []))
        return clients

    def list_active_hostnames(self):
        entries = []
        if not self.running or not self.server:
            return entries
        with self.server.data_lock:
            for hostname, peers in self.server.active_clients.items():
                for info in peers:
                    entries.append(
                        {
                            "hostname": hostname,
                            "ip": info.get("ip"),
                            "port": info.get("port"),
                        }
                    )
        entries.sort(
            key=lambda item: (
                item.get("hostname") or "",
                item.get("ip") or "",
                item.get("port") or 0,
            )
        )
        return entries


class ServerUI:
    def __init__(self, root, auto_start=False):
        self.root = root
        self.controller = ServerController()
        self.log_queue = queue.Queue()
        self.log_handler = QueueHandler(self.log_queue)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.INFO)

        self.ip_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="9999")
        self.db_var = tk.StringVar(value="server_data.json")

        self.active_clients_after_id = None
        self._active_clients_cache = []

        self._build_ui()
        self._poll_log_queue()

        if auto_start:
            self.root.after(100, self.start_server)

    def _build_ui(self):
        self.root.title("P2P Server Control")
        self.root.configure(bg=PASTEL_BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        header = tk.Label(
            self.root,
            text="Server Controller",
            bg=PASTEL_BG,
            fg="#5f0f40",
            font=("Segoe UI", 16, "bold"),
        )
        header.pack(padx=10, pady=(10, 5))

        config_frame = tk.Frame(self.root, bg=PASTEL_BG, bd=1, relief=tk.GROOVE)
        config_frame.pack(fill=tk.X, padx=10, pady=5)

        self._add_labeled_entry(config_frame, "IP Address:", self.ip_var, row=0)
        self._add_labeled_entry(config_frame, "Port:", self.port_var, row=1)
        self._add_labeled_entry(config_frame, "Database file:", self.db_var, row=2)

        button_frame = tk.Frame(self.root, bg=PASTEL_BG)
        button_frame.pack(fill=tk.X, padx=10, pady=5)

        self.start_button = tk.Button(
            button_frame,
            text="Start Server",
            command=self.start_server,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        self.start_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))

        self.stop_button = tk.Button(
            button_frame,
            text="Stop Server",
            command=self.stop_server,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))

        cmd_frame = tk.LabelFrame(self.root, text="Active Clients", bg=PASTEL_BG, fg="#5f0f40")
        cmd_frame.pack(fill=tk.BOTH, padx=10, pady=5)

        list_container = tk.Frame(cmd_frame, bg=PASTEL_BG)
        list_container.grid(row=0, column=0, rowspan=3, padx=5, pady=5, sticky="nsew")

        self.clients_listbox = tk.Listbox(
            list_container,
            height=8,
            bg="#fff7fb",
            relief=tk.FLAT,
            selectmode=tk.SINGLE,
            exportselection=False,
        )
        self.clients_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        clients_scrollbar = tk.Scrollbar(list_container, command=self.clients_listbox.yview)
        clients_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.clients_listbox.configure(yscrollcommand=clients_scrollbar.set)

        cmd_frame.columnconfigure(0, weight=1)
        cmd_frame.columnconfigure(1, weight=0)
        cmd_frame.rowconfigure(0, weight=1)
        cmd_frame.rowconfigure(1, weight=1)
        cmd_frame.rowconfigure(2, weight=1)

        self.refresh_clients_button = tk.Button(
            cmd_frame,
            text="Refresh List",
            command=self.refresh_active_clients,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
            state=tk.DISABLED,
        )
        self.refresh_clients_button.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        discover_button = tk.Button(
            cmd_frame,
            text="Discover Files",
            command=self.discover_selected_client,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        discover_button.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        ping_button = tk.Button(
            cmd_frame,
            text="Ping Client",
            command=self.ping_selected_client,
            bg=PASTEL_BUTTON,
            activebackground=PASTEL_ACCENT,
            relief=tk.FLAT,
        )
        ping_button.grid(row=2, column=1, padx=5, pady=5, sticky="ew")

        log_header = tk.Frame(self.root, bg=PASTEL_BG)
        log_header.pack(fill=tk.X, padx=10, pady=(10, 0))

        log_label = tk.Label(
            log_header,
            text="Server Log",
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

    def start_server(self):
        ip = self.ip_var.get().strip()
        port_value = self.port_var.get().strip()
        db_file = self.db_var.get().strip()

        if not ip or not port_value:
            messagebox.showerror("Invalid input", "IP address and port are required.")
            return

        try:
            port = int(port_value)
        except ValueError:
            messagebox.showerror("Invalid input", "Port must be an integer.")
            return

        try:
            self.controller.start(ip, port, db_file)
        except Exception as exc:
            messagebox.showerror("Error starting server", str(exc))
            logging.error("Failed to start server: %s", exc)
            return

        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.refresh_clients_button.config(state=tk.NORMAL)
        logging.info("Server UI ready. Listening on %s:%s", ip, port)
        self.refresh_active_clients()
        self._start_active_clients_poll()

    def stop_server(self):
        try:
            self.controller.stop()
        except Exception as exc:
            messagebox.showerror("Error stopping server", str(exc))
            logging.error("Failed to stop server: %s", exc)
            return

        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.refresh_clients_button.config(state=tk.DISABLED)
        self._stop_active_clients_poll()
        self._update_active_clients_list([])

    def discover_selected_client(self):
        entry = self._get_selected_client_entry()
        if not entry:
            messagebox.showinfo("Discover", "Select a client from the list first.")
            return
        hostname = entry.get("hostname")

        try:
            files = self.controller.discover(hostname)
        except Exception as exc:
            messagebox.showerror("Discover error", str(exc))
            logging.error("Discover failed: %s", exc)
            return

        if files:
            logging.info("Files published by %s: %s", hostname, ", ".join(files))
        else:
            logging.info("No files found for client %s.", hostname)

    def ping_selected_client(self):
        entry = self._get_selected_client_entry()
        if not entry:
            messagebox.showinfo("Ping", "Select a client from the list first.")
            return
        hostname = entry.get("hostname")

        try:
            clients = self.controller.ping(hostname)
        except Exception as exc:
            messagebox.showerror("Ping error", str(exc))
            logging.error("Ping failed: %s", exc)
            return

        if clients:
            logging.info(
                "PING: Client %s is ONLINE with %d connection(s).",
                hostname,
                len(clients),
            )
            for client_info in clients:
                logging.info("- %s:%s", client_info.get("ip"), client_info.get("port"))
        else:
            logging.info("PING: Client %s is OFFLINE.", hostname)

    def refresh_active_clients(self):
        try:
            entries = self.controller.list_active_hostnames()
        except Exception as exc:
            logging.error("Failed to load active clients: %s", exc)
            entries = []
        self._update_active_clients_list(entries)

    def _update_active_clients_list(self, entries):
        entries = list(entries)
        if entries == self._active_clients_cache:
            return
        current_entry = self._get_selected_client_entry()
        self._active_clients_cache = entries
        self.clients_listbox.delete(0, tk.END)
        for entry in entries:
            ip = entry.get("ip") or "?"
            port = entry.get("port")
            port_display = port if port is not None else "?"
            display = f"{entry.get('hostname')} ({ip}:{port_display})"
            self.clients_listbox.insert(tk.END, display)
        if current_entry and current_entry in entries:
            index = entries.index(current_entry)
            self.clients_listbox.selection_set(index)
            self.clients_listbox.activate(index)

    def _get_selected_client_entry(self):
        selection = self.clients_listbox.curselection()
        if not selection:
            return None
        index = selection[0]
        if 0 <= index < len(self._active_clients_cache):
            return self._active_clients_cache[index]
        return None

        self.clients_listbox.delete(0, tk.END)
        for hostname in hostnames:
            self.clients_listbox.insert(tk.END, hostname)
        if current_selection and current_selection in hostnames:
            index = hostnames.index(current_selection)
            self.clients_listbox.selection_set(index)
            self.clients_listbox.activate(index)

    def clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _start_active_clients_poll(self):
        self._stop_active_clients_poll()
        self._poll_active_clients()

    def _poll_active_clients(self):
        self.refresh_active_clients()
        if self.controller.running:
            self.active_clients_after_id = self.root.after(2000, self._poll_active_clients)

    def _stop_active_clients_poll(self):
        if self.active_clients_after_id is not None:
            self.root.after_cancel(self.active_clients_after_id)
            self.active_clients_after_id = None

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
        self._stop_active_clients_poll()
        self.controller.stop()
        self.refresh_clients_button.config(state=tk.DISABLED)
        logging.getLogger().removeHandler(self.log_handler)
        self.root.destroy()


def main(auto_start=False):
    root = tk.Tk()
    ServerUI(root, auto_start=auto_start)
    root.mainloop()


if __name__ == "__main__":
    main()
