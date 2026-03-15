#!/usr/bin/env -S python3 -u

import datetime
import http.server
import json
import os
import signal
import struct
import subprocess
import sys
import time
import traceback
import urllib
import urllib.request

ADDR = "0.0.0.0"
PORT = 19633
PROCESS_STARTUP_WAIT = 5

# AnkiConnect configuration - update these if running on a different machine
ANKICONNECT_HOST = "100.115.222.119"  # Windows Tailscale IP where Anki is running
ANKICONNECT_PORT = 8765

YOMITAN_API_NATIVE_MESSAGING_VERSION = 1
YOMITAN_VERSION = "25.12.16.0"  # Minimum version required by asbplayer
BLACKLISTED_PATHS = ["favicon.ico"]

script_path = os.path.realpath(os.path.dirname(__file__))
crowbarfile_path = script_path + "/.crowbar"

def error_log(message: str, error: str = "") -> None:
    try:
        utc_time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        with open(script_path + "/error.log", "a", encoding = "utf8") as log_file:
            log_file.write(utc_time + ", " + str(message).replace("\r", r"\r").replace("\n", r"\n") + ", " + str(error).replace("\r", r"\r").replace("\n", r"\n") + "\n")
    except Exception:
        # This exception cannot be "last resort" printed due to stdout being used for nativemessaging
        pass

def ensure_single_instance() -> None:
    wait_time = 0
    try:
        with open(crowbarfile_path, "r") as crowbarfile:
            pid = int(crowbarfile.read().strip())
            try:
                # Try SIGTERM first (works on POSIX)
                if hasattr(signal, 'SIGTERM'):
                    os.kill(pid, signal.SIGTERM)
                else:
                    # Windows: use taskkill or direct termination
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], 
                                 capture_output=True, timeout=2)
            except (OSError, ValueError, FileNotFoundError, subprocess.TimeoutExpired):
                # Process doesn't exist or already terminated
                pass
            wait_time = PROCESS_STARTUP_WAIT
    except FileNotFoundError:
        # First run or no prior instance
        pass
    except Exception:
        error_log(traceback.format_exc())

    with open(crowbarfile_path, "w") as crowbarfile:
        crowbarfile.write(str(os.getpid()))

    time.sleep(wait_time)

def delete_crowbarfile() -> None:
    os.remove(crowbarfile_path)

def get_message() -> dict:
    try:
        # Check if stdin is a terminal (interactive mode) - if so, no native messaging
        if sys.stdin.isatty():
            return None
        
        raw_length = sys.stdin.buffer.read(4)
        if not raw_length:
            return None
        message_length = struct.unpack("@I", raw_length)[0]
        message = sys.stdin.buffer.read(message_length).decode("utf-8")
        return json.loads(message)
    except (EOFError, OSError, KeyboardInterrupt, Exception):
        # stdin is not available or error occurred (HTTP mode, not native messaging)
        return None

def send_message(message_content: dict) -> None:
    encoded_content = json.dumps(message_content).encode("utf-8")
    encoded_length = struct.pack("@I", len(encoded_content))
    sys.stdout.buffer.write(encoded_length)
    sys.stdout.buffer.write(encoded_content)
    sys.stdout.buffer.flush()

def send_response(request_handler, status_code: int, content_type: str, data: str) -> None:
    request_handler.send_response(status_code)
    request_handler.send_header("Content-type", content_type)
    request_handler.send_header("Access-Control-Allow-Origin", "*")
    request_handler.send_header("Access-Control-Allow-Methods", "*")
    request_handler.send_header("Access-Control-Allow-Headers", "*")
    request_handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    request_handler.end_headers()
    request_handler.wfile.write(bytes(data, "utf-8"))

def proxy_to_ankiconnect(endpoint: str, body: str) -> dict:
    """Proxy a request to AnkiConnect and return the response"""
    try:
        url = f"http://{ANKICONNECT_HOST}:{ANKICONNECT_PORT}"
        
        # Parse the incoming request body
        try:
            body_data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            body_data = {}
        
        # Handle ankiFields endpoint - this needs to call AnkiConnect's ankiFieldsOnTemplate
        if endpoint == "ankiFields":
            # ankiFields returns empty for now since AnkiConnect doesn't have this exact endpoint
            # asbplayer mainly needs this to not error out
            return {"fields": []}
        
        # For other endpoints, try to forward to AnkiConnect
        anki_request = {"action": endpoint, "version": 6, "params": body_data}
        
        req = urllib.request.Request(
            url,
            data=json.dumps(anki_request).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.URLError as e:
        error_log(f"Error connecting to AnkiConnect ({endpoint})", str(e))
        return {"error": f"Cannot connect to AnkiConnect at {ANKICONNECT_HOST}:{ANKICONNECT_PORT}"}
    except Exception as e:
        error_log(f"Error proxying to AnkiConnect ({endpoint})", traceback.format_exc())
        return {"error": str(e)}

def handle_invalid_method(request_handler) -> None:
    request_handler.send_error(405, str(request_handler.command) + " method not allowed, only POST is accepted") # Method Not Allowed
    request_handler.send_header("Allow", "POST")
    request_handler.end_headers()

class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default HTTP logging
        pass

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path[1:]
        
        # Only allow test and serverVersion via GET
        if path in ["test", "serverVersion", ""]:
            send_response(self, 200, "application/json", json.dumps({"version": YOMITAN_API_NATIVE_MESSAGING_VERSION}))
            return
        
        # Reject other GET requests
        self.send_error(405, "GET method not allowed, only POST is accepted")
        self.send_header("Allow", "POST")
        self.end_headers()

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path[1:]
        # Parse query string and convert lists to single values
        query_params = urllib.parse.parse_qs(parsed_url.query)
        params = {key: value[0] if len(value) == 1 else value for key, value in query_params.items()}
        content_length = int(self.headers["Content-Length"] or 0)
        body = self.rfile.read(content_length).decode("utf-8")

        if path in BLACKLISTED_PATHS:
            send_response(self, 400, "", "")
            return

        if path in ["serverVersion", "", "test"]:
            send_response(self, 200, "application/json", json.dumps({"version": YOMITAN_API_NATIVE_MESSAGING_VERSION}))
            return

        # Handle yomitanVersion specially to return app version
        if path == "yomitanVersion":
            send_response(self, 200, "application/json", json.dumps({"version": YOMITAN_VERSION}))
            return

        # Handle common endpoints that may be called in HTTP mode (e.g., by asbplayer)
        if path == "ankiFields":
            result = proxy_to_ankiconnect("ankiFields", body)
            send_response(self, 200, "application/json", json.dumps(result, ensure_ascii=False))
            return
        
        if path == "termEntries":
            send_response(self, 200, "application/json", json.dumps({"entries": []}))
            return
        
        if path == "kanjiEntries":
            send_response(self, 200, "application/json", json.dumps({"entries": []}))
            return
        
        if path == "tokenize":
            send_response(self, 200, "application/json", json.dumps({"tokens": []}))
            return

        try:
            send_message({"action": path, "params": params, "body": body})
            yomitan_response = get_message()
            
            if yomitan_response is None:
                # No native messaging available; return a test response
                send_response(self, 200, "application/json", json.dumps({"api": "yomitan", "version": YOMITAN_API_NATIVE_MESSAGING_VERSION}))
                return
                
            send_response(self, yomitan_response["responseStatusCode"], "application/json", json.dumps(yomitan_response["data"], ensure_ascii = False))
        except Exception as e:
            error_log(f"Error processing request for path '{path}'", traceback.format_exc())
            send_response(self, 500, "application/json", json.dumps({"error": str(e)}))

    # Override other HTTP methods to reject them
    def do_HEAD(self) -> None:
        self.send_error(405, "HEAD method not allowed")
    def do_PUT(self) -> None:
        self.send_error(405, "PUT method not allowed")
    def do_DELETE(self) -> None:
        self.send_error(405, "DELETE method not allowed")
    def do_CONNECT(self) -> None:
        self.send_error(405, "CONNECT method not allowed")
    def do_OPTIONS(self) -> None:
        # Handle CORS preflight requests
        send_response(self, 200, "application/json", "")
    def do_TRACE(self) -> None:
        self.send_error(405, "TRACE method not allowed")
    def do_PATCH(self) -> None:
        self.send_error(405, "PATCH method not allowed")

try:
    ensure_single_instance()
    httpd = http.server.HTTPServer((ADDR, PORT), RequestHandler)
    httpd.serve_forever()
    delete_crowbarfile()
except Exception:
    error_log(traceback.format_exc())
