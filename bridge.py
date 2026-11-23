"""Local HTTP queue for native GIF generation and import."""

import argparse
import base64
import hmac
import io
import json
import os
import queue
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

CHATGPT = "https://chatgpt.com/"
JOB_PATH = re.compile(r"/v1/gif/jobs/([0-9a-f]{32})(/result)?")
MAX_BODY = 32_768
MAX_GIF = 25 * 1024 * 1024
MAX_JOBS = 8
MAX_PENDING = 4
IMAGE_API = "https://api.openai.com/v1/images/generations"


class JobError(Exception):
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


def generate_with_api(prompt, upstream_key):
    payload = json.dumps({
        "model": "gpt-image-2.6-flare", "output_format": "gif",
        "prompt": prompt, "stream": False, "n": 1,
    }).encode()
    request = urllib.request.Request(IMAGE_API, data=payload, headers={
        "Authorization": "Bearer " + upstream_key, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            limit = (MAX_GIF * 4 // 3) + 64 * 1024
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise JobError("The Images API response was too large.")
        encoded = json.loads(raw)["data"][0]["b64_json"]
        return animated_gif(base64.b64decode(encoded, validate=True))
    except urllib.error.HTTPError as error:
        with error:
            raise JobError(f"The OpenAI Images API returned HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError):
        raise JobError("The OpenAI Images API could not be reached or timed out.") from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise JobError("The Images API returned an invalid result.") from None


@dataclass
class Job:
    status: str = "queued"
    result: bytes | None = None
    error: str | None = None
    prompt: str | None = None


class Jobs:
    def __init__(self):
        self.items = OrderedDict()
        self.pending = queue.Queue()
        self.lock = threading.Lock()

    def submit(self, prompt, backend):
        with self.lock:
            if sum(job.status in ("queued", "running", "awaiting_upload") for job in self.items.values()) >= MAX_PENDING:
                raise JobError("The queue is full.")
            if len(self.items) >= MAX_JOBS:
                oldest = next(key for key, job in self.items.items() if job.status in ("completed", "failed"))
                del self.items[oldest]
            job_id = secrets.token_hex(16)
            if backend == "web":
                self.items[job_id] = Job(status="awaiting_upload", prompt=prompt)
            else:
                self.items[job_id] = Job()
                self.pending.put((job_id, prompt))
            return job_id

    def get(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            return Job(job.status, job.result, job.error, job.prompt) if job else None

    def import_gif(self, job_id, data):
        with self.lock:
            job = self.items.get(job_id)
            if job is None:
                return 404
            if job.status != "awaiting_upload":
                return 409
            self.items[job_id] = Job(status="completed", result=data)
            return 200

    def cancel(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            if job is None:
                return 404
            if job.status != "awaiting_upload":
                return 409
            del self.items[job_id]
            return 200

    def run(self, generate):
        while True:
            item = self.pending.get()
            if item is None:
                return
            job_id, prompt = item
            with self.lock:
                self.items[job_id].status = "running"
            try:
                result = Job(status="completed", result=generate(prompt))
            except JobError as error:
                result = Job(status="failed", error=str(error))
            except Exception as error:
                print(f"Images API operation failed ({type(error).__name__}).", file=sys.stderr)
                result = Job(status="failed", error="The Images API operation failed.")
            with self.lock:
                self.items[job_id] = result


def http_handler(jobs, token, api_enabled=False):
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
            backend = data.get("backend", "web")
            if backend not in ("web", "api"):
                return self.reply_json(400, error="Backend must be web or api.")
            if backend == "api" and not api_enabled:
                return self.reply_json(503, error="Set OPENAI_API_KEY to enable the API backend.")
            try:
                job_id = jobs.submit(prompt.strip(), backend)
            except JobError as error:
                return self.reply_json(429, error=str(error))
            self.reply_json(202, id=job_id, status="awaiting_upload" if backend == "web" else "queued")

        def do_PUT(self):
            if not self.authorized():
                return
            match = JOB_PATH.fullmatch(self.path)
            if not match or not match[2]:
                return self.reply_json(404, error="Unknown endpoint.")
            job = jobs.get(match[1])
            if job is None:
                return self.reply_json(404, error="Job not found.")
            if job.status != "awaiting_upload":
                return self.reply_json(409, error="The job is not awaiting a web GIF.")
            if self.headers.get_content_type() != "image/gif":
                return self.reply_json(415, error="Use image/gif.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply_json(400, error="Invalid Content-Length.")
            if length > MAX_GIF:
                return self.reply_json(413, error="The upload is too large.")
            if length <= 0:
                return self.reply_json(400, error="A GIF body is required.")
            data = self.rfile.read(length)
            if len(data) != length:
                return self.reply_json(400, error="The upload was incomplete.")
            try:
                result = animated_gif(data)
            except JobError as error:
                return self.reply_json(422, error=str(error))
            status = jobs.import_gif(match[1], result)
            if status != 200:
                return self.reply_json(status, error="The job is no longer awaiting a GIF.")
            self.reply_json(200, id=match[1], status="completed")

        def do_DELETE(self):
            if not self.authorized():
                return
            match = JOB_PATH.fullmatch(self.path)
            if not match or match[2]:
                return self.reply_json(404, error="Unknown endpoint.")
            status = jobs.cancel(match[1])
            if status != 200:
                return self.reply_json(status, error="No web job awaiting a GIF at this ID.")
            self.reply_json(200, id=match[1], status="cancelled")

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/health":
                return self.reply_json(200, status="ok")
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

    upstream_key = os.environ.get("OPENAI_API_KEY", "")
    jobs = Jobs()
    with ThreadingHTTPServer(("127.0.0.1", args.port), http_handler(jobs, token, bool(upstream_key))) as server:
        if upstream_key:
            worker = threading.Thread(
                target=jobs.run, args=(lambda prompt: generate_with_api(prompt, upstream_key),), daemon=True,
            )
            worker.start()
        print(f"Listening on http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
