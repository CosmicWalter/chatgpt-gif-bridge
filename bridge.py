"""Local headless browser bridge for ChatGPT GIF generation."""

import argparse
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, asdict, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from browser_worker import BrowserError, BrowserWorker
from openai_compat import APIError, MODEL, generate, validate_request
from result_store import ResultStore
from urllib.parse import urlsplit, parse_qs

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
    phase: str = "queued"
    chat_url: str | None = None
    created_at: float = field(default_factory=time.time)


class Jobs:
    def __init__(self, directory=None):
        self.items = OrderedDict()
        self.lock = threading.Lock()
        self.directory = Path(directory) if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for path in sorted(self.directory.glob('*.json'), key=lambda path: path.stat().st_mtime):
                if not re.fullmatch(r'[0-9a-f]{32}', path.stem):
                    continue
                try:
                    data = json.loads(path.read_text())
                    job = Job(**data)
                    result = path.with_suffix('.gif')
                    if job.status == 'completed':
                        job.result = animated_gif(result.read_bytes())
                    self.items[path.stem] = job
                except (ValueError, TypeError, OSError):
                    continue
            self.items = OrderedDict(sorted(self.items.items(), key=lambda entry: entry[1].created_at))

    def _save(self, job_id):
        if not self.directory:
            return
        job = self.items[job_id]
        if job.result is not None:
            result = self.directory / (job_id + '.gif')
            result.write_bytes(job.result)
            result.chmod(0o600)
        data = asdict(job)
        data['result'] = None
        path = self.directory / (job_id + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        temporary.chmod(0o600)
        temporary.replace(path)

    def _remove(self, job_id):
        del self.items[job_id]
        if self.directory:
            for suffix in ('.gif', '.json'):
                (self.directory / (job_id + suffix)).unlink(missing_ok=True)

    def submit(self, prompt):
        with self.lock:
            if sum(job.status in ("queued", "running", "waiting_verification") for job in self.items.values()) >= MAX_PENDING:
                raise JobError("The queue is full.")
            if len(self.items) >= MAX_JOBS:
                oldest = next(key for key, job in self.items.items() if job.status in ("completed", "failed"))
                self._remove(oldest)
            job_id = secrets.token_hex(16)
            self.items[job_id] = Job(prompt=prompt)
            self._save(job_id)
            return job_id

    def get(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            return Job(**asdict(job)) if job else None

    def update(self, job_id, **fields):
        with self.lock:
            job = self.items[job_id]
            for field, value in fields.items():
                setattr(job, field, value)
            self._save(job_id)

    def cancel(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            if job is None:
                return 404
            if job.status in ("queued", "running", "waiting_verification"):
                return 409
            self._remove(job_id)
            return 200

    def list(self):
        with self.lock:
            return [{"id": key, "status": job.status, "prompt": job.prompt, "error": job.error} for key, job in reversed(self.items.items())]


def http_handler(jobs, token, browser, results=None):
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
            if self.path.startswith(("/v1/images/", "/v1/models")):
                self.reply_json(401, error={"message": "A valid bearer token is required.", "type": "authentication_error", "code": "invalid_api_key", "param": None})
            else:
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
            if self.path == "/v1/images/generations":
                return self.create_image()
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

        def create_image(self):
            try:
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "Use application/json.", "invalid_content_type")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise APIError(400, "Invalid Content-Length.", "invalid_request") from None
                if not 0 < length <= MAX_BODY:
                    raise APIError(413 if length > MAX_BODY else 400, "Invalid request size.", "invalid_request")
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, RecursionError):
                    raise APIError(400, "Invalid JSON.", "invalid_json") from None
                prompt = validate_request(data)
                try:
                    result = generate(jobs, browser, prompt,
                                      response_format=data.get("response_format", "b64_json"),
                                      results=results,
                                      base_url=f"http://127.0.0.1:{self.server.server_port}")
                except BrowserError as error:
                    raise APIError(503, str(error), getattr(error, "code", "browser_unavailable")) from None
                except JobError as error:
                    raise APIError(429, str(error), "queue_full") from None
                return self.reply_json(200, **result)
            except APIError as error:
                return self.reply_json(error.status, error=error.error)

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
            parsed = urlsplit(self.path)
            result_match = re.fullmatch(r'/v1/images/results/([0-9a-f]{32})\.gif', parsed.path)
            if result_match:
                query = parse_qs(parsed.query)
                try:
                    expires = int(query.get('expires', [''])[0])
                    signature = query.get('signature', [''])[0]
                    data = results.read(result_match[1], expires, signature) if results else None
                except (ValueError, OverflowError):
                    data = None
                if data is None:
                    return self.reply_json(403, error={"message": "Invalid or expired download link.", "type": "invalid_request_error", "code": "invalid_download_link", "param": None})
                return self.reply(200, data, "image/gif")
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path in assets:
                name, content_type = assets[self.path]
                data = (Path(__file__).resolve().parent / "web" / name).read_bytes()
                return self.reply(200, data, content_type)
            if not self.authorized():
                return
            if self.path == "/v1/models":
                return self.reply_json(200, object="list", data=[{"id": MODEL, "object": "model", "created": 0, "owned_by": "local-bridge"}])
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

    jobs = Jobs(args.state / "jobs")
    browser = BrowserWorker(args.state / "browser", jobs, animated_gif)
    results = ResultStore(args.state / "downloads", token)
    with ThreadingHTTPServer(("127.0.0.1", args.port), http_handler(jobs, token, browser, results)) as server:
        browser.thread.start()
        print(f"Listening on http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            browser.close()
            results.close()


if __name__ == "__main__":
    main()
