import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright
from bridge import Jobs, animated_gif
from browser_worker import BrowserError, BrowserWorker
from result_store import ResultStore
from test_bridge import fixture_image


class RecoveryTests(unittest.TestCase):
    def wait(self, predicate, description):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail(description)

    @contextmanager
    def browser_fixture(self, challenge, sends, launches):
        animation = fixture_image()
        def route_page(route):
            url = route.request.url
            if url.endswith('/sent'):
                sends.append('submitted')
                return route.fulfill(body='ok')
            if challenge.is_set():
                return route.fulfill(content_type='text/html', body='<title>Just a moment...</title><div id="challenge-running">Verification required</div>')
            if url.endswith('/animation.gif'):
                return route.fulfill(body=animation, headers={'Content-Type':'image/gif','Content-Disposition':'attachment'})
            if '/c/' in url:
                return route.fulfill(content_type='text/html', body='<textarea id="prompt-textarea"></textarea><article data-message-author-role="assistant"><a href="/animation.gif">Download GIF</a></article>')
            route.fulfill(content_type='text/html', body='''<textarea id="prompt-textarea"></textarea>
                <button data-testid="send-button" onclick="fetch('/sent',{method:'POST'}).then(()=>location.href='/c/fixture-chat')">Send</button>''')
        @contextmanager
        def routed():
            with sync_playwright() as playwright:
                launch = playwright.chromium.launch_persistent_context
                def launch_routed(*args, **kwargs):
                    launches.append(kwargs['headless'])
                    kwargs['headless'] = True
                    context = launch(*args, **kwargs)
                    context.route('https://chatgpt.com/**', route_page)
                    return context
                playwright.chromium.launch_persistent_context = launch_routed
                yield playwright
        with patch('browser_worker.sync_playwright', routed):
            yield

    def test_verification_pauses_then_resumes_without_duplicate_submission(self):
        challenge = threading.Event()
        sends, launches = [], []
        with tempfile.TemporaryDirectory() as directory, self.browser_fixture(challenge, sends, launches):
            jobs = Jobs(Path(directory) / 'jobs')
            worker = BrowserWorker(Path(directory) / 'browser', jobs, animated_gif)
            worker.thread.start()
            try:
                self.wait(lambda: worker.snapshot()['status']=='ready', 'Browser did not become ready')
                challenge.set()
                job_id = worker.submit('boat')
                self.wait(lambda: worker.snapshot()['status']=='verification_required', 'Challenge not classified')
                self.assertEqual(jobs.get(job_id).status, 'waiting_verification')
                self.assertEqual(sends, [])
                with self.assertRaises(BrowserError) as denied:
                    worker.submit('duplicate')
                self.assertEqual(denied.exception.code, 'verification_required')
                worker.login()
                self.wait(lambda: worker.snapshot()['status']=='signing_in', 'Login window not opened')
                self.assertFalse(worker.snapshot()['headless'])
                challenge.clear()  # Simulates the human completing verification.
                worker.finish_login()
                self.wait(lambda: jobs.get(job_id).status=='completed', 'Original job did not resume')
                self.assertTrue(worker.snapshot()['headless'])
                self.assertEqual(sends, ['submitted'])
                self.assertEqual(launches, [True, False, True])
                self.assertEqual(jobs.get(job_id).result, fixture_image())
            finally:
                worker.close()

    def test_restart_resumes_saved_chat_without_resending_prompt(self):
        sends, launches = [], []
        with tempfile.TemporaryDirectory() as directory, self.browser_fixture(threading.Event(), sends, launches):
            path = Path(directory) / 'jobs'
            jobs = Jobs(path)
            job_id = jobs.submit('original')
            jobs.update(job_id, status='running', phase='sent', chat_url='https://chatgpt.com/c/original-chat')
            recovered = Jobs(path)
            worker = BrowserWorker(Path(directory) / 'browser', recovered, animated_gif)
            worker.thread.start()
            try:
                self.wait(lambda: recovered.get(job_id).status=='completed', 'Saved job did not complete')
                self.assertEqual(sends, [])
                self.assertEqual(recovered.get(job_id).chat_url, 'https://chatgpt.com/c/original-chat')
                self.assertEqual(Jobs(path).get(job_id).result, fixture_image())
            finally:
                worker.close()

    def test_uncertain_submission_is_never_sent_again(self):
        sends, launches = [], []
        with tempfile.TemporaryDirectory() as directory, self.browser_fixture(threading.Event(), sends, launches):
            jobs = Jobs()
            job_id = jobs.submit('original')
            jobs.update(job_id, status='running', phase='submitting')
            worker = BrowserWorker(Path(directory) / 'browser', jobs, animated_gif)
            worker.thread.start()
            try:
                self.wait(lambda: jobs.get(job_id).status=='failed', 'Uncertain submission not stopped')
                self.assertEqual(sends, [])
                self.assertIn('未重新提交', jobs.get(job_id).error)
            finally:
                worker.close()

    def test_signed_result_survives_restart_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            job_id = 'a' * 32
            data = fixture_image()
            store = ResultStore(directory, 'test-key')
            url = store.save(job_id, data, 'http://127.0.0.1:18763')
            query = parse_qs(urlsplit(url).query)
            expires = int(query['expires'][0])
            signature = query['signature'][0]
            store.close()
            store = ResultStore(directory, 'test-key')
            try:
                self.assertEqual(store.read(job_id, expires, signature), data)
                self.assertIsNone(store.read('b'*32, expires, signature))
                with patch('result_store.time.time', return_value=expires+1):
                    self.assertIsNone(store.read(job_id, expires, signature))
                    store.cleanup()
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                store.close()

    def test_restart_preserves_fifo_despite_progress_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Jobs(directory)
            first = jobs.submit('first')
            second = jobs.submit('second')
            jobs.update(first, status='running', phase='sent', chat_url='https://chatgpt.com/c/first')
            self.assertEqual([row['id'] for row in reversed(Jobs(directory).list())], [first, second])

    def test_network_failure_is_not_misreported_as_login_or_success(self):
        with tempfile.TemporaryDirectory() as directory:
            @contextmanager
            def disconnected():
                with sync_playwright() as playwright:
                    launch = playwright.chromium.launch_persistent_context
                    def launch_offline(*args, **kwargs):
                        context = launch(*args, **kwargs)
                        context.route('https://chatgpt.com/**', lambda route: route.abort('internetdisconnected'))
                        return context
                    playwright.chromium.launch_persistent_context = launch_offline
                    yield playwright
            with patch('browser_worker.sync_playwright', disconnected):
                jobs = Jobs()
                worker = BrowserWorker(Path(directory) / 'browser', jobs, animated_gif)
                worker.thread.start()
                try:
                    self.wait(lambda: worker.snapshot()['status']=='error', 'Network failure not classified')
                    with self.assertRaises(BrowserError):
                        worker.submit('boat')
                    self.assertEqual(jobs.list(), [])
                finally:
                    worker.close()


if __name__ == '__main__':
    unittest.main()
