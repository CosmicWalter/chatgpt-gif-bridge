import base64
import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image

from bridge import IMAGE_API, Jobs, generate_with_api, http_handler


def fixture_image(animated=True):
    output = io.BytesIO()
    image = Image.new("RGB", (8, 8), "red")
    if animated:
        image.save(output, "GIF", save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")])
    else:
        image.save(output, "PNG")
    return output.getvalue()


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.jobs = Jobs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_handler(self.jobs, "test-token", True))
        self.listener = threading.Thread(target=self.server.serve_forever)
        self.listener.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.listener.join)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, method=None, media="application/json", auth=True):
        headers = {"Content-Type": media}
        if auth:
            headers["Authorization"] = "Bearer test-token"
        url = f"http://127.0.0.1:{self.server.server_port}{path}"
        return urllib.request.urlopen(
            urllib.request.Request(url, headers=headers, data=data, method=method), timeout=5,
        )

    def job(self, backend):
        with self.request("/v1/gif/jobs", json.dumps({"prompt": "Test motion", "backend": backend}).encode()) as response:
            self.assertEqual(response.status, 202)
            return "/v1/gif/jobs/" + json.load(response)["id"]

    def test_web_job_returns_original_animation_and_rejects_still_image(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/health", auth=False)
        self.assertEqual(denied.exception.code, 401)
        denied.exception.close()
        path = self.job("web")
        with self.request(path) as response:
            job = json.load(response)
        self.assertEqual(job["status"], "awaiting_upload")
        self.assertEqual(job["prompt"], "Test motion")
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request(path + "/result", fixture_image(False), method="PUT", media="image/gif")
        self.assertEqual(rejected.exception.code, 422)
        rejected.exception.close()
        animation = fixture_image()
        with self.request(path + "/result", animation, method="PUT", media="image/gif") as uploaded:
            self.assertEqual(json.load(uploaded)["status"], "completed")
        with self.request(path + "/result") as response:
            self.assertEqual(response.headers.get_content_type(), "image/gif")
            self.assertEqual(response.read(), animation)

    def test_api_job_requests_native_gif_and_returns_original_bytes(self):
        path = self.job("api")
        animation = fixture_image()
        response = {"data": [{"b64_json": base64.b64encode(animation).decode()}]}

        def mock_provider(request, timeout):
            self.assertEqual(request.full_url, IMAGE_API)
            self.assertEqual(request.get_header("Authorization"), "Bearer upstream-test")
            body = json.loads(request.data)
            self.assertEqual(body["model"], "gpt-image-2.6-flare")
            self.assertEqual(body["output_format"], "gif")
            self.assertFalse(body["stream"])
            return io.BytesIO(json.dumps(response).encode())

        self.jobs.pending.put(None)
        with patch("bridge.urllib.request.urlopen", mock_provider):
            self.jobs.run(lambda prompt: generate_with_api(prompt, "upstream-test"))
        with self.request(path + "/result") as result:
            self.assertEqual(result.read(), animation)


if __name__ == "__main__":
    unittest.main()
