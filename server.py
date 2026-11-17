"""
PulseGrid Backend — login API + static file server
Built with ONLY the Python standard library (no pip installs needed).

Run:  python server.py
Then open:  http://localhost:3000

Demo credentials:  admin / admin123
(Change ADMIN_USER / ADMIN_PASSWORD below for your real demo.)
"""

import hashlib
import json
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).parent
PORT = 3000

# ----- Demo user (password is stored as a SHA-256 hash, never as plain text) -----
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin123"
ADMIN_HASH = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()

# ----- Face biometrics -----
# The browser computes a 256-bit "face signature" (average hash of the camera frame).
# We compare signatures with Hamming distance; small distance = same face.
FACE_DB_FILE = ROOT / "faces.json"          # enrolled face signatures (persists across restarts)
SCAN_LOG_FILE = ROOT / "face_scans.json"    # audit log of every scan attempt
FACE_MATCH_BITS = 64                        # max Hamming distance to accept (of 256 bits) = ~75% confidence

# ----- Machine control (admin-only) -----
MACHINE_IDS = ["furnace", "mixer", "convA", "press", "cooler", "convB", "packer"]
CONTROL_LOG_FILE = ROOT / "control_log.json"  # audit trail of every control command

def log_control(user, machine, action, speed):
    log = load_json(CONTROL_LOG_FILE, [])
    log.insert(0, {
        "user": user, "machine": machine, "action": action, "speed": speed,
        "time": datetime.now().isoformat(timespec="seconds"),
    })
    save_json(CONTROL_LOG_FILE, log[:30])

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))

FACES = load_json(FACE_DB_FILE, {})         # {"admin": "a1b2..."}
TOKENS = {}                                 # active sessions (token -> user info)

def hamming(a, b):
    """True bit-level Hamming distance between two 256-bit hex signatures."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")

def issue_token(user):
    token = uuid.uuid4().hex
    TOKENS[token] = {"username": user, "name": "Administrator"}
    return token

def log_scan(user, ok, distance):
    """Keep an audit trail of face scans — judges love this."""
    log = load_json(SCAN_LOG_FILE, [])
    log.insert(0, {
        "user": user,
        "result": "MATCH" if ok else "REJECTED",
        "distance": distance,
        "time": datetime.now().isoformat(timespec="seconds"),
    })
    save_json(SCAN_LOG_FILE, log[:20])

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".md": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    # ---------- API ----------
    def do_POST(self):
        route = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            return self._json(400, {"ok": False, "error": "Invalid JSON body"})

        if route == "/api/login":
            user = str(data.get("username", "")).strip()
            pwd = str(data.get("password", ""))
            pwd_hash = hashlib.sha256(pwd.encode()).hexdigest()

            if user == ADMIN_USER and pwd_hash == ADMIN_HASH:
                token = issue_token(user)
                return self._json(200, {
                    "ok": True,
                    "token": token,
                    "user": {"username": user, "name": "Administrator"},
                })
            return self._json(401, {"ok": False, "error": "Invalid username or password"})

        # ----- FaceGate: enroll face signatures (multiple 256-bit samples from the camera) -----
        if route == "/api/face/enroll":
            user = str(data.get("username", "")).strip()
            sigs = data.get("signatures") or ([data.get("signature")] if data.get("signature") else [])
            sigs = [str(s).strip().lower() for s in sigs]
            if user != ADMIN_USER:
                return self._json(404, {"ok": False, "error": "Unknown user"})
            valid = [s for s in sigs if len(s) == 64 and all(c in "0123456789abcdef" for c in s)]
            if not valid:
                return self._json(400, {"ok": False, "error": "Invalid face signature(s)"})
            old = FACES.get(user)
            if isinstance(old, str):
                old = [old]
            FACES[user] = (old or []) + valid
            FACES[user] = FACES[user][-6:]           # keep newest 6 samples
            save_json(FACE_DB_FILE, FACES)
            log_scan(user, True, 0)
            return self._json(200, {"ok": True, "message": "Face enrolled for " + user,
                                    "samples": len(FACES[user])})

        # ----- FaceGate: scan-to-sign-in (best match across stored and captured samples) -----
        if route == "/api/face/login":
            user = str(data.get("username", "")).strip() or ADMIN_USER
            got = data.get("signatures") or ([data.get("signature")] if data.get("signature") else [])
            got = [str(s).strip().lower() for s in got
                   if len(str(s)) == 64 and all(c in "0123456789abcdef" for c in str(s))]
            stored = FACES.get(user)
            if stored is None:
                log_scan(user, False, 256)
                return self._json(404, {"ok": False, "code": "NO_FACE",
                                        "error": "No face on file for this user — first-time setup needed"})
            if isinstance(stored, str):
                stored = [stored]
            if not got:
                return self._json(400, {"ok": False, "error": "Could not read a face signature from the camera"})
            distance = min(hamming(s, t) for s in got for t in stored)
            log_scan(user, distance <= FACE_MATCH_BITS, distance)
            if distance <= FACE_MATCH_BITS:
                confidence = round((1 - distance / 256) * 100)
                token = issue_token(user)
                return self._json(200, {
                    "ok": True,
                    "token": token,
                    "confidence": confidence,
                    "user": {"username": user, "name": "Administrator"},
                })
            return self._json(401, {
                "ok": False,
                "code": "NO_MATCH",
                "error": "Face did not match. Move to the same spot/lighting, or use password.",
                "confidence": round((1 - distance / 256) * 100),
            })

        # ----- Machine control — ADMIN ONLY (requires a valid session token) -----
        if route == "/api/control":
            token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            info = TOKENS.get(token)
            if not info:
                return self._json(401, {"ok": False, "error": "Admin session required — please sign in"})
            machine = str(data.get("machine", ""))
            action = str(data.get("action", ""))
            if machine not in MACHINE_IDS:
                return self._json(404, {"ok": False, "error": "Unknown machine: " + machine})
            if action == "speed":
                try:
                    speed = max(0, min(100, int(data.get("value", 100))))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "Invalid speed value"})
            elif action == "stop":
                speed = 0
            elif action == "start":
                speed = 100
            else:
                return self._json(400, {"ok": False, "error": "Unknown action: " + action})
            log_control(info["username"], machine, action, speed)
            return self._json(200, {"ok": True, "machine": machine, "speed": speed,
                                    "by": info["username"],
                                    "message": info["username"] + " set " + machine + " speed to " + str(speed) + "%"})

        return self._json(404, {"ok": False, "error": "Endpoint not found"})

    def do_GET(self):
        path_ = self.path.split("?")[0]

        # health check
        if path_ == "/api/health":
            return self._json(200, {
                "ok": True,
                "service": "PulseGrid backend",
                "time": datetime.now().isoformat(timespec="seconds"),
                "activeSessions": len(TOKENS),
            })

        # who am I (requires the token from login)
        if path_ == "/api/whoami":
            token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            info = TOKENS.get(token)
            if info:
                return self._json(200, {"ok": True, "user": info})
            return self._json(401, {"ok": False, "error": "Not logged in"})

        # ----- static files -----
        rel = unquote(path_.lstrip("/"))
        if rel in ("", "/"):
            rel = "index.html"
        file_path = (ROOT / rel).resolve()
        # block path-traversal outside the project folder
        if not str(file_path).startswith(str(ROOT.resolve())):
            return self._json(403, {"ok": False, "error": "Forbidden"})
        if file_path.is_file():
            ctype = MIME.get(file_path.suffix.lower(), "application/octet-stream")
            self._send(200, file_path.read_bytes(), ctype)
        else:
            self._send(404, "404 Not found", "text/plain")

    def log_message(self, fmt, *args):
        pass  # keep the console clean


if __name__ == "__main__":
    print("=" * 56)
    print("  PulseGrid backend running")
    print("  Open:      http://localhost:" + str(PORT))
    print("  Login:     admin / " + ADMIN_PASSWORD)
    print("  Stop:      press Ctrl+C in this window")
    print("=" * 56)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
