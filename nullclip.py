#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import platform
import queue
import select
import shutil
import socket
import subprocess
import sys
import threading
import time

try:
    import readline
except ImportError:
    readline = None


VERSION = "2.2"

HOST = "0.0.0.0"
PORT = 45873

MAX_CLIPBOARD_SIZE = 8 * 1024 * 1024
CONNECT_TIMEOUT = 8
CLIPBOARD_INTERVAL = 0.25


# ============================================================
# Clipboard
# ============================================================

class Clipboard:

    @staticmethod
    def read():
        system = platform.system()

        try:
            if system == "Darwin":
                result = subprocess.run(
                    ["pbpaste"],
                    capture_output=True,
                    timeout=2
                )

                if result.returncode != 0:
                    return None

                return result.stdout

            if system == "Windows":
                command = [
                    "powershell.exe",
                    "-NoProfile",
                    "-STA",
                    "-Command",
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                    "$x=Get-Clipboard -Raw; "
                    "[Console]::OpenStandardOutput().Write("
                    "[System.Text.Encoding]::UTF8.GetBytes($x),0,"
                    "([System.Text.Encoding]::UTF8.GetByteCount($x)))"
                ]

                result = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=3
                )

                if result.returncode != 0:
                    return None

                return result.stdout

            if system == "Linux":
                if shutil.which("wl-paste"):
                    result = subprocess.run(
                        ["wl-paste", "--no-newline"],
                        capture_output=True,
                        timeout=2
                    )
                    return result.stdout

                if shutil.which("xclip"):
                    result = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-o"],
                        capture_output=True,
                        timeout=2
                    )
                    return result.stdout

                if shutil.which("xsel"):
                    result = subprocess.run(
                        ["xsel", "--clipboard", "--output"],
                        capture_output=True,
                        timeout=2
                    )
                    return result.stdout

        except Exception:
            return None

        return None

    @staticmethod
    def write(data):
        system = platform.system()

        try:
            if system == "Darwin":
                result = subprocess.run(
                    ["pbcopy"],
                    input=data,
                    timeout=3
                )

                return result.returncode == 0

            if system == "Windows":
                encoded = base64.b64encode(data).decode("ascii")

                script = (
                    "$b=[Convert]::FromBase64String("
                    "'"
                    + encoded
                    + "'"
                    ");"
                    "$s=[System.Text.Encoding]::UTF8.GetString($b);"
                    "Set-Clipboard -Value $s"
                )

                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-STA",
                        "-Command",
                        script
                    ],
                    capture_output=True,
                    timeout=5
                )

                return result.returncode == 0

            if system == "Linux":
                if shutil.which("wl-copy"):
                    result = subprocess.run(
                        ["wl-copy"],
                        input=data,
                        timeout=3
                    )
                    return result.returncode == 0

                if shutil.which("xclip"):
                    result = subprocess.run(
                        ["xclip", "-selection", "clipboard"],
                        input=data,
                        timeout=3
                    )
                    return result.returncode == 0

                if shutil.which("xsel"):
                    result = subprocess.run(
                        ["xsel", "--clipboard", "--input"],
                        input=data,
                        timeout=3
                    )
                    return result.returncode == 0

        except Exception:
            return False

        return False


# ============================================================
# NullClip
# ============================================================

class NullClip:

    def __init__(self):
        self.running = True

        self.server = None
        self.peer = None
        self.peer_addr = None

        self.pending_request = None

        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()

        self.last_clipboard_hash = None
        self.last_sent_hash = None

        self.cli_input_queue = queue.Queue()

    # --------------------------------------------------------
    # Output
    # --------------------------------------------------------

    def log(self, message):
        """
        Print asynchronous messages without creating another
        prompt.

        readline handles restoring the current input line.
        """

        if readline is not None and os.name != "nt":
            try:
                current = readline.get_line_buffer()

                sys.stdout.write("\r\033[2K")
                sys.stdout.write(message + "\n")
                sys.stdout.write("nullclip> " + current)

                sys.stdout.flush()
                readline.redisplay()

                return

            except Exception:
                pass

        sys.stdout.write("\r\033[2K")
        sys.stdout.write(message + "\n")
        sys.stdout.write("nullclip> ")
        sys.stdout.flush()

    # --------------------------------------------------------
    # Screen
    # --------------------------------------------------------

    @staticmethod
    def clear_terminal():
        if os.name == "nt":
            os.system("cls")
        else:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    def startup_screen(self):
        self.clear_terminal()

        print()
        print("╔════════════════════════════════════════════════════════════════════╗")
        print("║                              NULLCLIP                              ║")
        print("╚════════════════════════════════════════════════════════════════════╝")
        print()
        print(f"  Version : {VERSION}")
        print(f"  Port    : {PORT}")
        print()
        print("──────────────────────────────────────────────────────────────────────")
        print()
        print("  Commands:")
        print()
        print("    connect <IP> [PORT]   Connect to a peer")
        print("    disconnect            Disconnect")
        print("    status                Show connection status")
        print("    help                  Show help")
        print("    exit / quit           Exit NullClip")
        print()
        print("──────────────────────────────────────────────────────────────────────")
        print()

    # --------------------------------------------------------
    # Start
    # --------------------------------------------------------

    def start(self):
        self.startup_screen()

        self.start_server()

        clipboard_thread = threading.Thread(
            target=self.clipboard_loop,
            daemon=True
        )

        clipboard_thread.start()

        print("[INFO] NullClip is ready.")
        print()

        self.cli_loop()

    # --------------------------------------------------------
    # Server
    # --------------------------------------------------------

    def start_server(self):
        self.server = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        self.server.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1
        )

        self.server.bind((HOST, PORT))
        self.server.listen(10)

        thread = threading.Thread(
            target=self.accept_loop,
            daemon=True
        )

        thread.start()

    def accept_loop(self):
        while self.running:
            try:
                connection, address = self.server.accept()

                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_KEEPALIVE,
                    1
                )

                thread = threading.Thread(
                    target=self.handle_incoming,
                    args=(connection, address),
                    daemon=True
                )

                thread.start()

            except OSError:
                break

            except Exception as exc:
                if self.running:
                    self.log(f"[ERROR] Accept failed: {exc}")

    # --------------------------------------------------------
    # Incoming connection
    # --------------------------------------------------------

    def handle_incoming(self, connection, address):

        with self.state_lock:
            already_connected = self.peer is not None
            request_pending = self.pending_request is not None

        if already_connected or request_pending:
            self.send_json(connection, {
                "type": "busy"
            })

            connection.close()
            return

        try:
            self.send_json(connection, {
                "type": "request"
            })

            with self.state_lock:
                self.pending_request = (
                    connection,
                    address
                )

            self.log("")
            self.log("Incoming NullClip connection")
            self.log(f"From: {address[0]}:{address[1]}")
            self.log("")
            self.log("Allow this peer?")
            self.log("[y] Accept")
            self.log("[n] Reject")

        except Exception:
            try:
                connection.close()
            except Exception:
                pass

    # --------------------------------------------------------
    # Outgoing connection
    # --------------------------------------------------------

    def connect(self, host, port):

        with self.state_lock:
            if self.peer is not None:
                self.log("[ERROR] Already connected.")
                return

        thread = threading.Thread(
            target=self.outgoing_connect,
            args=(host, port),
            daemon=True
        )

        thread.start()

    def outgoing_connect(self, host, port):

        connection = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        connection.settimeout(CONNECT_TIMEOUT)

        try:
            self.log(f"[INFO] Connecting to {host}:{port}...")

            connection.connect((host, port))

            connection.settimeout(None)

            message = self.recv_json(connection)

            if not message:
                raise ConnectionError("Peer closed the connection.")

            message_type = message.get("type")

            if message_type == "busy":
                self.log("[INFO] Peer is busy.")
                connection.close()
                return

            if message_type != "request":
                raise ConnectionError(
                    "Invalid response from peer."
                )

            self.send_json(connection, {
                "type": "accept"
            })

            with self.state_lock:
                self.peer = connection
                self.peer_addr = (host, port)

            self.log(
                f"[INFO] Connected to {host}:{port}."
            )

            self.peer_receive_loop(
                connection,
                (host, port)
            )

        except Exception as exc:
            self.log(
                f"[ERROR] Connection failed: {exc}"
            )

            try:
                connection.close()
            except Exception:
                pass

    # --------------------------------------------------------
    # Pending request
    # --------------------------------------------------------

    def process_pending_request(self, command):

        with self.state_lock:
            request = self.pending_request

        if request is None:
            return False

        command = command.lower().strip()

        if command in (
            "y",
            "yes",
            "accept"
        ):
            connection, address = request

            with self.state_lock:
                self.pending_request = None
                self.peer = connection
                self.peer_addr = address

            try:
                self.send_json(connection, {
                    "type": "accept"
                })

                self.log(
                    f"[INFO] Connected to "
                    f"{address[0]}:{address[1]}."
                )

                thread = threading.Thread(
                    target=self.peer_receive_loop,
                    args=(connection, address),
                    daemon=True
                )

                thread.start()

            except Exception as exc:
                self.log(
                    f"[ERROR] Failed to accept connection: {exc}"
                )

                self.disconnect()

            return True

        if command in (
            "n",
            "no",
            "reject"
        ):
            connection, address = request

            with self.state_lock:
                self.pending_request = None

            try:
                self.send_json(connection, {
                    "type": "reject"
                })

                connection.close()

            except Exception:
                pass

            self.log(
                f"[INFO] Rejected connection from "
                f"{address[0]}:{address[1]}."
            )

            return True

        return False

    # --------------------------------------------------------
    # Disconnect
    # --------------------------------------------------------

    def disconnect(self):

        with self.state_lock:
            connection = self.peer

            self.peer = None
            self.peer_addr = None

        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

            try:
                connection.close()
            except Exception:
                pass

            self.log("[INFO] Disconnected.")

    # --------------------------------------------------------
    # Peer receive
    # --------------------------------------------------------

    def peer_receive_loop(self, connection, address):

        buffer = b""

        try:
            while self.running:

                data = connection.recv(65536)

                if not data:
                    break

                buffer += data

                while b"\n" in buffer:
                    line, buffer = buffer.split(
                        b"\n",
                        1
                    )

                    if not line:
                        continue

                    try:
                        message = json.loads(
                            line.decode("utf-8")
                        )

                    except Exception:
                        continue

                    self.handle_network_message(
                        message
                    )

        except Exception:
            pass

        finally:
            with self.state_lock:
                if self.peer is connection:
                    self.peer = None
                    self.peer_addr = None

            try:
                connection.close()
            except Exception:
                pass

            if self.running:
                self.log(
                    f"[INFO] Connection to "
                    f"{address[0]}:{address[1]} closed."
                )

    # --------------------------------------------------------
    # Network message
    # --------------------------------------------------------

    def handle_network_message(self, message):

        message_type = message.get("type")

        if message_type == "clipboard":

            encoded = message.get("data")

            if not encoded:
                return

            try:
                data = base64.b64decode(
                    encoded,
                    validate=True
                )

            except Exception:
                self.log(
                    "[ERROR] Received invalid clipboard data."
                )
                return

            if len(data) > MAX_CLIPBOARD_SIZE:
                self.log(
                    "[ERROR] Clipboard data is too large."
                )
                return

            digest = hashlib.sha256(data).hexdigest()

            self.last_clipboard_hash = digest
            self.last_sent_hash = digest

            if Clipboard.write(data):
                self.log(
                    "[CLIPBOARD] Clipboard updated."
                )

    # --------------------------------------------------------
    # JSON transport
    # --------------------------------------------------------

    def send_json(self, connection, message):

        data = (
            json.dumps(
                message,
                separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )

        with self.send_lock:
            connection.sendall(data)

    def recv_json(self, connection):

        buffer = b""

        while True:

            data = connection.recv(65536)

            if not data:
                return None

            buffer += data

            if b"\n" in buffer:

                line, _ = buffer.split(
                    b"\n",
                    1
                )

                return json.loads(
                    line.decode("utf-8")
                )

    # --------------------------------------------------------
    # Clipboard sync
    # --------------------------------------------------------

    def clipboard_loop(self):

        while self.running:

            try:
                data = Clipboard.read()

                if data is not None:

                    if len(data) <= MAX_CLIPBOARD_SIZE:

                        digest = hashlib.sha256(
                            data
                        ).hexdigest()

                        if (
                            digest != self.last_clipboard_hash
                            and digest != self.last_sent_hash
                        ):

                            self.last_clipboard_hash = digest

                            with self.state_lock:
                                connection = self.peer

                            if connection is not None:

                                encoded = base64.b64encode(
                                    data
                                ).decode("ascii")

                                try:
                                    self.send_json(
                                        connection,
                                        {
                                            "type": "clipboard",
                                            "data": encoded
                                        }
                                    )

                                    self.last_sent_hash = digest

                                except Exception:
                                    pass

            except Exception:
                pass

            time.sleep(
                CLIPBOARD_INTERVAL
            )

    # --------------------------------------------------------
    # CLI
    # --------------------------------------------------------

    def cli_loop(self):

        if os.name == "nt":
            self.cli_loop_windows()
        else:
            self.cli_loop_posix()

    # --------------------------------------------------------
    # POSIX CLI
    # --------------------------------------------------------

    def cli_loop_posix(self):

        # Print the prompt ONCE.
        sys.stdout.write("nullclip> ")
        sys.stdout.flush()

        while self.running:

            try:

                readable, _, _ = select.select(
                    [sys.stdin],
                    [],
                    [],
                    0.5
                )

                if not readable:
                    # IMPORTANT:
                    # Do absolutely nothing here.
                    # No prompt. No redraw.
                    continue

                line = sys.stdin.readline()

                if not line:
                    break

                command = line.strip()

                # Remove the current line.
                sys.stdout.write("\r\033[2K")
                sys.stdout.flush()

                if not command:
                    sys.stdout.write("nullclip> ")
                    sys.stdout.flush()
                    continue

                # Incoming approval gets priority.
                if self.process_pending_request(command):
                    sys.stdout.write("nullclip> ")
                    sys.stdout.flush()
                    continue

                self.handle_command(command)

                if self.running:
                    sys.stdout.write("nullclip> ")
                    sys.stdout.flush()

            except KeyboardInterrupt:
                self.shutdown()
                break

            except Exception as exc:
                self.log(
                    f"[ERROR] CLI error: {exc}"
                )

    # --------------------------------------------------------
    # Windows CLI
    # --------------------------------------------------------

    def cli_loop_windows(self):

        def reader():

            while self.running:

                try:
                    command = input("nullclip> ")

                    self.cli_input_queue.put(
                        command
                    )

                except EOFError:
                    self.cli_input_queue.put(
                        "exit"
                    )
                    break

                except KeyboardInterrupt:
                    self.cli_input_queue.put(
                        "exit"
                    )
                    break

        thread = threading.Thread(
            target=reader,
            daemon=True
        )

        thread.start()

        while self.running:

            try:
                command = self.cli_input_queue.get(
                    timeout=0.2
                )

                if self.process_pending_request(command):
                    continue

                self.handle_command(command)

            except queue.Empty:
                continue

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    def handle_command(self, command):

        parts = command.split()

        if not parts:
            return

        cmd = parts[0].lower()

        # CONNECT
        if cmd == "connect":

            if len(parts) < 2:
                self.log(
                    "[ERROR] Usage: connect <IP> [PORT]"
                )
                return

            host = parts[1]

            port = PORT

            if len(parts) >= 3:
                try:
                    port = int(parts[2])

                except ValueError:
                    self.log(
                        "[ERROR] Invalid port."
                    )
                    return

            self.connect(
                host,
                port
            )

            return

        # DISCONNECT
        if cmd == "disconnect":
            self.disconnect()
            return

        # STATUS
        if cmd == "status":

            with self.state_lock:
                peer = self.peer
                address = self.peer_addr
                pending = self.pending_request

            if peer is not None and address is not None:

                self.log(
                    f"[STATUS] Connected to "
                    f"{address[0]}:{address[1]}"
                )

            elif pending is not None:

                _, address = pending

                self.log(
                    f"[STATUS] Incoming connection "
                    f"from {address[0]}:{address[1]}"
                )

            else:

                self.log(
                    "[STATUS] Not connected."
                )

            return

        # HELP
        if cmd == "help":

            self.log("")
            self.log("Commands:")
            self.log("")
            self.log(
                "  connect <IP> [PORT]   Connect to a peer"
            )
            self.log(
                "  disconnect            Disconnect"
            )
            self.log(
                "  status                Show connection status"
            )
            self.log(
                "  help                  Show help"
            )
            self.log(
                "  exit / quit           Exit NullClip"
            )
            self.log("")

            return

        # EXIT
        if cmd in (
            "exit",
            "quit"
        ):

            self.shutdown()
            return

        self.log(
            f"[ERROR] Unknown command: {command}"
        )

    # --------------------------------------------------------
    # Shutdown
    # --------------------------------------------------------

    def shutdown(self):

        if not self.running:
            return

        self.running = False

        try:
            if self.server:
                self.server.close()
        except Exception:
            pass

        with self.state_lock:
            peer = self.peer
            self.peer = None
            self.peer_addr = None

        if peer is not None:
            try:
                peer.shutdown(
                    socket.SHUT_RDWR
                )
            except Exception:
                pass

            try:
                peer.close()
            except Exception:
                pass

        with self.state_lock:
            pending = self.pending_request
            self.pending_request = None

        if pending is not None:
            try:
                pending[0].close()
            except Exception:
                pass

        print()
        print(
            "──────────────────────────────────────────────────────────────"
        )
        print("NullClip has been closed.")
        print(
            "──────────────────────────────────────────────────────────────"
        )


# ============================================================
# Main
# ============================================================

def main():

    app = NullClip()

    try:
        app.start()

    except KeyboardInterrupt:
        app.shutdown()

    except Exception as exc:
        print()
        print(f"[FATAL] {exc}")
        app.shutdown()


if __name__ == "__main__":
    main()