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

    def connect(self, server_ip, server_port, p2p_port, client_name=None):
        with self._lock:
            if self.connected:
                raise RuntimeError("Client already connected.")

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

    def publish(self, local_path, alias):
        if not self.connected or not self.client:
            raise RuntimeError("Client is not connected.")
        with self._socket_lock:
            self.client._do_publish(local_path, alias)

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

    def download_from_peer(self, peer_info, destination_path):
        if not self.connected or not self.client:
            raise RuntimeError("Client is not connected.")
        self.client._download_from_peer(peer_info, destination_path)

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

        self._build_ui()
        self._poll_log_queue()

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

        threading.Thread(
            target=self._connect_task,
            args=(server_ip, server_port, p2p_port, client_name_value or None),
            daemon=True,
        ).start()

    def _connect_task(self, server_ip, server_port, p2p_port, client_name):
        try:
            response = self.controller.connect(server_ip, server_port, p2p_port, client_name)
        except Exception as exc:
            logging.error("Connection failed: %s", exc)
            self.root.after(0, lambda: self._on_connect_failed(str(exc)))
            return

        self.root.after(0, lambda: self._on_connected(client_name, response))

    def _on_connected(self, client_name, response):
        self.disconnect_button.config(state=tk.NORMAL)
        display_name = client_name or socket.gethostname()
        logging.info("Client UI is connected to the server as %s.", display_name)

    def _on_connect_failed(self, message):
        self.connect_button.config(state=tk.NORMAL)
        messagebox.showerror("Connection error", message)

    def disconnect_from_server(self):
        self.controller.disconnect()
        self.connect_button.config(state=tk.NORMAL)
        self.disconnect_button.config(state=tk.DISABLED)

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
            args=(local_path, alias),
            daemon=True,
        ).start()

    def _publish_task(self, local_path, alias):
        try:
            self.controller.publish(local_path, alias)
        except Exception as exc:
            logging.error("Publish failed: %s", exc)
            self.root.after(0, lambda: messagebox.showerror("Publish error", str(exc)))
            return
        logging.info("Publish request sent for %s -> alias '%s'", local_path, alias)

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
            hostname = peer.get("hostname") or "Unknown"
            original_name = os.path.basename(peer.get("lname") or fname)
            listbox.insert(
                tk.END,
                f"{idx}. {hostname} ({original_name})",
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

        custom_button = tk.Button(button_frame, text="Custom...", command=on_custom, bg=PASTEL_BUTTON)
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
