"""Local headless browser bridge for ChatGPT GIF generation."""

import argparse
import hmac
import io
import json
import os
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from browser_worker import BrowserError, BrowserWorker

CHATGPT = "https://chatgpt.com/"
JOB_PATH = re.compile(r"/v1/gif/jobs/([0-9a-f]{32})(/result)?")
MAX_BODY = 32_768
MAX_GIF = 25 * 1024 * 1024
MAX_JOBS = 8
MAX_PENDING = 4


class JobError(ValueError):
    """An error safe to return to the caller."""


def animated_gif(data):
    if len(data) > MAX_GIF:
        raise JobError("The result exceeded the 25 MiB limit.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "GIF" or image.n_frames < 2:
                raise JobError("Upload an animated GIF with at least two frames.")
            if image.n_frames > 256 or image.width * image.height * image.n_frames > 64_000_000:
                raise JobError("The GIF exceeds the frame or decoded pixel limit.")
            for frame in range(image.n_frames):
                image.seek(frame)
                image.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise JobError("The GIF could not be decoded.") from None
    return data


@dataclass
class Job:
    status: str = "queued"
    result: bytes | None = None
    error: str | None = None
    prompt: str | None = None


class Jobs:
    def __init__(self):
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def submit(self, prompt):
        with self.lock:
            if sum(job.status in ("queued", "running") for job in self.items.values()) >= MAX_PENDING:
                raise JobError("The queue is full.")
            if len(self.items) >= MAX_JOBS:
                oldest = next(key for key, job in self.items.items() if job.status in ("completed", "failed"))
                del self.items[oldest]
            job_id = secrets.token_hex(16)
            self.items[job_id] = Job(prompt=prompt)
            return job_id

    def get(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            return Job(job.status, job.result, job.error, job.prompt) if job else None

    def update(self, job_id, **fields):
        with self.lock:
            job = self.items[job_id]
            for field, value in fields.items():
                setattr(job, field, value)

    def cancel(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            if job is None:
                return 404
            if job.status in ("queued", "running"):
                return 409
            del self.items[job_id]
            return 200

    def list(self):
        with self.lock:
            return [{"id": key, "status": job.status, "prompt": job.prompt, "error": job.error} for key, job in reversed(self.items.items())]


def http_handler(jobs, token, browser):
    expected = ("Bearer " + token).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def reply(self, status, data, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
            if content_type == "image/gif":
                self.send_header("Content-Disposition", 'attachment; filename="animation.gif"')
            self.end_headers()
            self.wfile.write(data)

        def reply_json(self, http_status, **data):
            self.reply(http_status, json.dumps(data).encode(), "application/json")

        def authorized(self):
            if hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                return True
            self.reply_json(401, error="A valid bearer token is required.")
            return False

        def do_POST(self):
            if not self.authorized():
                return
            if self.path in ("/session/login", "/session/finish"):
                try:
                    if self.path.endswith("/login"):
                        browser.login()
                    else:
                        browser.finish_login()
                except BrowserError as error:
                    return self.reply_json(409, error=str(error))
                return self.reply_json(202, **browser.snapshot())
            if self.path != "/v1/gif/jobs":
                return self.reply_json(404, error="Unknown endpoint.")
            if self.headers.get_content_type() != "application/json":
                return self.reply_json(415, error="Use application/json.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply_json(400, error="Invalid Content-Length.")
            if length > MAX_BODY:
                return self.reply_json(413, error="Request is too large.")
            if length <= 0:
                return self.reply_json(400, error="A request body is required.")
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return self.reply_json(400, error="Invalid JSON.")
            prompt = data.get("prompt") if isinstance(data, dict) else None
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8_000:
                return self.reply_json(400, error="Provide a prompt of 1 to 8000 characters.")
            if "backend" in data:
                return self.reply_json(400, error="仅支持 ChatGPT 无头浏览器生成，请移除 backend 参数。")
            try:
                job_id = browser.submit(prompt.strip())
            except BrowserError as error:
                return self.reply_json(409, error=str(error))
            except JobError as error:
                return self.reply_json(429, error=str(error))
            self.reply_json(202, id=job_id, status="queued")

        def do_DELETE(self):
            if not self.authorized():
                return
            match = JOB_PATH.fullmatch(self.path)
            if not match or match[2]:
                return self.reply_json(404, error="Unknown endpoint.")
            status = jobs.cancel(match[1])
            if status != 200:
                return self.reply_json(status, error="The job cannot be removed while queued or running.")
            self.reply_json(200, id=match[1], status="cancelled")

        def do_GET(self):
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path in assets:
                name, content_type = assets[self.path]
                data = (Path(__file__).resolve().parent / "web" / name).read_bytes()
                return self.reply(200, data, content_type)
            if not self.authorized():
                return
            if self.path == "/health":
                return self.reply_json(200, status="ok", mode="headless")
            if self.path == "/session":
                return self.reply_json(200, **browser.snapshot())
            if self.path == "/v1/gif/jobs":
                return self.reply_json(200, jobs=jobs.list())
            match = JOB_PATH.fullmatch(self.path)
            job = jobs.get(match[1]) if match else None
            if job is None:
                return self.reply_json(404, error="Job not found.")
            if match[2]:
                if job.result is None:
                    return self.reply_json(409, status=job.status, error=job.error or "Result is not ready.")
                return self.reply(200, job.result, "image/gif")
            fields = {"id": match[1], "status": job.status}
            if job.prompt:
                fields.update(chatgpt_url=CHATGPT, prompt=job.prompt)
            if job.error:
                fields["error"] = job.error
            if job.result:
                fields["result_url"] = f"/v1/gif/jobs/{match[1]}/result"
            self.reply_json(200, **fields)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(__file__).resolve().parent / ".state")
    parser.add_argument("--port", type=int, default=18763)
    args = parser.parse_args()
    os.umask(0o077)
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path = args.state / "api-token"
    try:
        with token_path.open("x") as file:
            file.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    token = token_path.read_text().strip()
    if not re.fullmatch("[0-9a-f]{64}", token):
        raise SystemExit("Invalid local API token file.")

    jobs = Jobs()
    browser = BrowserWorker(args.state / "browser", jobs, animated_gif)
    with ThreadingHTTPServer(("127.0.0.1", args.port), http_handler(jobs, token, browser)) as server:
        browser.thread.start()
        print(f"Listening on http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            browser.close()


if __name__ == "__main__":
    main()
