"""
arXiv Digest — Email Relay

Vercel serverless function that sends digest emails via Gmail SMTP.
Deployed once by the project maintainer. Forks call this relay so users
never need to configure SMTP credentials.

Env vars (set in Vercel dashboard, never in code):
  SMTP_USER       — Gmail address (e.g. arxivdigestau@gmail.com)
  SMTP_PASSWORD   — Gmail App Password
  RELAY_TOKEN     — shared token to prevent abuse (must match digest.py)
"""
from __future__ import annotations

import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler

SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "").strip()

MAX_RECIPIENTS = 20


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler."""

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"error": "invalid JSON"})
            return

        # ── Validate token ──
        token = body.get("token", "")
        if not RELAY_TOKEN or token != RELAY_TOKEN:
            self._respond(403, {"error": "invalid token"})
            return

        # ── Extract fields ──
        recipients = body.get("recipients", [])
        subject = body.get("subject", "")
        html = body.get("html", "")
        plain_text = body.get("plain_text", "")

        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]

        if not recipients or not html or not subject:
            self._respond(400, {"error": "missing recipients, subject, or html"})
            return

        if len(recipients) > MAX_RECIPIENTS:
            self._respond(400, {"error": f"max {MAX_RECIPIENTS} recipients"})
            return

        # ── Send via Gmail SMTP ──
        if not SMTP_USER or not SMTP_PASSWORD:
            self._respond(500, {"error": "relay SMTP not configured"})
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"arXiv Digest <{SMTP_USER}>"
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, recipients, msg.as_bytes())
            self._respond(200, {"ok": True, "sent_to": len(recipients)})
        except smtplib.SMTPAuthenticationError:
            self._respond(500, {"error": "SMTP auth failed"})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def do_GET(self):
        self._respond(200, {"status": "arXiv Digest relay is running"})

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass
