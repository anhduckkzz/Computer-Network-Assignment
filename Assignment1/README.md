# Assignment 1 – P2P File Sharing (Executable Notes)

This folder contains everything needed to package the existing server/client into standalone `.exe` builds while keeping the Tkinter UI experience. The original project code lives one level up and is unchanged.

---

## 1. Running From Source (Tkinter UI)

```bash
cd <repo>/Assignment1

# Server UI (auto-starts listener)
python server_exe.py

# Client UI (auto-increments port/name each launch)
python client_exe.py
```

Helpful flags (works for both scripts):

| Flag | Description |
| --- | --- |
| `--host/--port` (server) | Change server bind interface/port. |
| `--db-file <path>` (server) | Store SQLite metadata somewhere else; defaults to `p2p_metadata.db` next to the executable/script. |
| `--no-ui` (server) | Force CLI mode if you want the old terminal workflow. |
| `--server-ip/--server-port` (client) | Point the client at a different server. |
| `--p2p-port` / `--client-name` (client) | Override the auto-generated identity for this launch only. |
| `--reset-state` (client) | Clear `client_launch_state.json` so the next auto identity restarts at `1111/a`. |
| `--cli` (client) | Skip Tkinter entirely and use the CLI client loop. |
| `--auto-connect` (client) | Auto-press “Connect” when the UI opens. |
| `--log-level` | Choose `DEBUG`, `INFO`, … for either script. |

Notes:
* Every new client launch without overrides grabs the next `<port>/<name>` pair: `1111/a`, `2222/b`, `3333/c`, … (step of 1111 on ports, alphabetical naming). State is stored in `client_launch_state.json` alongside the executable/script.
* The server and client both suppress the noisy “shared files refresh” log entries, but the actual polling behaviour is still active.
* The server’s metadata is now backed by SQLite; a fresh DB file is created automatically the first time you launch it.

---

## 2. Building Standalone Executables

Run the following from `Assignment1` (repo root). PyInstaller 6+ is recommended.

```bash
# Server – windowed build (no console pop-up, Tkinter UI by default)
pyinstaller --clean --noconfirm --onefile --windowed \
  --name server \
  --paths . --paths exe \
  exe/server_exe.py

# Client – windowed build (UI only; CLI fallback still available with --cli)
pyinstaller --clean --noconfirm --onefile --windowed \
  --name client \
  --paths . --paths exe \
  exe/client_exe.py
```

Artifacts land in `dist/server.exe` and `dist/client.exe`. You can copy them anywhere; the scripts automatically detect whether they are running from source or as frozen binaries and adjust module paths plus data directories accordingly.

Runtime files created next to each executable:

* `p2p_metadata.db` – SQLite database for published metadata (server only). Use `--db-file D:\somewhere\files.db` if you want to relocate it.
* `client_launch_state.json` – remembers the next port/name combination for auto-generated clients.

---

## 3. Typical Workflow

1. **Start the server UI**  
   Double-click `server.exe` (or run `python server_exe.py`). It auto-starts the listener and shows connection logs. Use the “Stop Server” button to shut down gracefully.

2. **Launch clients**  
   Double-click `client.exe` as many times as you like. Each instance auto-fills the next port/name. Hit “Connect” (or pass `--auto-connect` when running via CLI) to register with the server.

3. **Publish files**  
   Choose a local file + alias in the client UI, press “Publish”, and the server immediately writes metadata to SQLite. If file contents change, publishing with the same alias updates its metadata.

4. **Shared files list**  
   Every connected client polls the server every 5 seconds using the `list_shared_files` action. The UI list refreshes automatically—no need to press the “Refresh” button unless you want to force it right away.

5. **Fetching**  
   Select a file from the shared list, click “Fetch”, pick a destination, and the client negotiates directly with the peer via its P2P listener.

6. **Close clients / server**  
   Disconnecting removes their entries, and the shared file list eventually reflects the change during the next poll. The server UI can be closed via the window controls or the “Stop Server” button.

---

## 4. Troubleshooting Tips

* **“Server not reachable”** – confirm `server.exe` is running, the firewall allows TCP 9999, and you entered the correct host/port in the client UI.
* **Auto identity got weird** – delete `client_launch_state.json` or run `client_exe.py --reset-state`. You can always override manually per launch.
* **Need verbose logging** – launch scripts with `--log-level DEBUG`. Polling spam stays suppressed by design, but everything else surfaces.
* **SQLite file locked** – close all running server processes; SQLite uses file-level locks. You can safely remove `p2p_metadata.db` to start fresh (data will be re-created on next launch).

---

That’s it! These wrappers keep the core assignment untouched while making it easy to run via Tkinter or distribute as executables. Let me know if you need variations for headless/bash-only environments. 😊
