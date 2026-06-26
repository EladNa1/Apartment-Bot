#!/usr/bin/env python3
"""Dashboard server. Run: python server.py → open http://localhost:8080"""
import http.server, json
from pathlib import Path

DIR = Path(__file__).parent
CFG = json.loads((DIR / "config.json").read_text(encoding="utf-8"))
PORT = CFG.get("dashboard_port", 8080)

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DIR), **kw)
    def do_GET(self):
        if self.path == "/api/apartments":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            p = DIR / "apartments.json"
            self.wfile.write(p.read_bytes() if p.exists() else b'{"apartments":[]}')
            return
        if self.path == "/api/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write((DIR / "config.json").read_bytes())
            return
        if self.path == "/":
            self.path = "/dashboard.html"
        super().do_GET()
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

if __name__ == "__main__":
    print(f"Dashboard → http://localhost:{PORT}")
    http.server.HTTPServer(("", PORT), H).serve_forever()
