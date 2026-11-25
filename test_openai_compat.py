import base64
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from bridge import Jobs, animated_gif, http_handler
from browser_worker import BrowserError
from result_store import ResultStore
import tempfile
from pathlib import Path
from openai_compat import APIError, MODEL, generate, validate_request
from test_bridge import fixture_image


class FixtureBrowser:
    """Deterministic browser boundary; never uses an extension or external account."""
    def __init__(self, jobs, validate):
        self.jobs, self.validate, self.ready = jobs, validate, False

    def connect(self, unused):
        self.ready = True

    def snapshot(self):
        return {"status": "ready" if self.ready else "login_required"}

    def submit(self, prompt):
        if not self.ready:
            raise BrowserError("Log in first")
        return self.jobs.submit(prompt)

    def next_job(self, unused):
        for row in reversed(self.jobs.list()):
            if row['status'] == 'queued':
                self.jobs.update(row['id'], status='running')
                return {'job': row}
        return {'job': None}

    def complete(self, unused, job_id, data):
        self.jobs.update(job_id, status='completed', result=self.validate(data))


class OpenAICompatTests(unittest.TestCase):
    def setUp(self):
        self.jobs = Jobs()
        self.worker = FixtureBrowser(self.jobs, animated_gif)
        self.client = 'browser-compat-test'
        self.directory = tempfile.TemporaryDirectory()
        self.results = ResultStore(Path(self.directory.name) / 'results', 'test-key')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), http_handler(self.jobs, 'test-key', self.worker, self.results))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.results.close()
        self.directory.cleanup()

    def request(self, path, body=None, key='test-key'):
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
        request = urllib.request.Request(self.base + path, headers=headers, data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_models_and_authentication(self):
        status, data = self.request('/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual(data['data'][0]['id'], MODEL)
        status, data = self.request('/v1/models', key='wrong')
        self.assertEqual(status, 401)
        self.assertEqual(data['error']['code'], 'invalid_api_key')

    def test_native_gif_returns_openai_envelope_and_original_bytes(self):
        self.worker.connect(self.client)
        original = fixture_image()
        failures = []
        def browser():
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = self.worker.next_job(self.client)['job']
                    if job:
                        self.worker.complete(self.client, job['id'], data=original)
                        return
                    time.sleep(.01)
                failures.append('No job received')
            except Exception as error:
                failures.append(str(error))
        browser_thread = threading.Thread(target=browser)
        browser_thread.start()
        status, result = self.request('/v1/images/generations', {'model': MODEL, 'prompt': 'A paper boat', 'n': 1, 'response_format': 'b64_json'})
        browser_thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(status, 200, result)
        self.assertIsInstance(result['created'], int)
        self.assertEqual(base64.b64decode(result['data'][0]['b64_json']), original)
        self.assertEqual(len(self.jobs.list()), 1)

    def test_disconnected_browser_is_503(self):
        status, data = self.request('/v1/images/generations', {'prompt': 'boat'})
        self.assertEqual(status, 503)
        self.assertEqual(data['error']['code'], 'browser_unavailable')
        self.assertEqual(self.jobs.list(), [])

    def test_size_and_quality_are_generation_hints(self):
        prompt = validate_request({'prompt': 'boat', 'size': '1024x1024', 'quality': 'high'})
        self.assertIn('1024x1024', prompt)
        self.assertIn('high', prompt)

    def test_sdk_b64_and_url_roundtrip(self):
        from openai import OpenAI
        self.worker.connect(self.client)
        original = fixture_image()
        stop = threading.Event()
        def complete_jobs():
            while not stop.wait(.01):
                job = self.worker.next_job(self.client)['job']
                if job:
                    self.worker.complete(self.client, job['id'], data=original)
        browser_thread = threading.Thread(target=complete_jobs)
        browser_thread.start()
        try:
            with OpenAI(base_url=self.base + '/v1', api_key='test-key', max_retries=0, timeout=5) as client:
                self.assertEqual(client.models.list().data[0].id, MODEL)
                for mode in ('b64_json', 'url'):
                    response = client.images.generate(model=MODEL, prompt='boat', n=1, response_format=mode, size='1024x1024', quality='high')
                    if mode == 'b64_json':
                        self.assertEqual(base64.b64decode(response.data[0].b64_json), original)
                    else:
                        url = response.data[0].url
                        self.assertNotIn('test-key', url)
                        with urllib.request.urlopen(url, timeout=3) as downloaded:
                            self.assertEqual(downloaded.read(), original)
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(url.replace('signature=', 'signature=0'), timeout=3)
                        self.assertEqual(denied.exception.code, 403)
                        denied.exception.close()
        finally:
            stop.set()
            browser_thread.join()

    def test_full_queue_is_429_without_an_extra_job(self):
        self.worker.connect(self.client)
        for index in range(4):
            self.worker.submit(str(index))
        status, data = self.request('/v1/images/generations', {'prompt': 'boat'})
        self.assertEqual(status, 429)
        self.assertEqual(data['error']['code'], 'queue_full')
        self.assertEqual(len(self.jobs.list()), 4)

    def test_unsupported_parameters_are_not_silently_ignored(self):
        for extra in ({'model': 'gpt-image-1'}, {'n': 2}, {'n': True}, {'response_format': 'unknown'}, {'output_format': 'png'}, {'size': 'invalid'}):
            with self.subTest(extra=extra):
                status, data = self.request('/v1/images/generations', {'prompt': 'boat', **extra})
                self.assertEqual(status, 400)
                self.assertIsInstance(data['error'], dict)
        self.assertEqual(self.jobs.list(), [])

    def test_timeout_does_not_submit_twice_or_cancel_original(self):
        self.worker.connect(self.client)
        with self.assertRaises(APIError) as raised:
            generate(self.jobs, self.worker, 'boat', timeout=0)
        self.assertEqual(raised.exception.status, 504)
        self.assertEqual(len(self.jobs.list()), 1)
        job = self.worker.next_job(self.client)['job']
        self.assertEqual(raised.exception.error['job_id'], job['id'])
        self.worker.complete(self.client, job['id'], data=fixture_image())
        self.assertEqual(self.jobs.get(job['id']).status, 'completed')

    def test_failed_job_has_no_successful_image(self):
        class FailingBrowser:
            def submit(inner, prompt):
                job_id = self.jobs.submit(prompt)
                self.jobs.update(job_id, status='failed', error='No native GIF returned')
                return job_id
        with self.assertRaises(APIError) as raised:
            generate(self.jobs, FailingBrowser(), 'boat')
        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.error['code'], 'generation_failed')


if __name__ == '__main__':
    unittest.main()
