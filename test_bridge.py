import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

from bridge import JobError, Jobs, animated_gif, http_handler
from browser_worker import BrowserWorker, ChatGPTBrowser


def fixture_image(animated=True):
    output = io.BytesIO()
    image = Image.new("RGB", (8, 8), "red")
    if animated:
        image.save(output, "GIF", save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")])
    else:
        image.save(output, "PNG")
    return output.getvalue()


class BridgeTest(unittest.TestCase):
    def test_visible_job_and_login_keep_one_browser_session(self):
        animation = fixture_image()
        logged_in = threading.Event()
        launched = []

        def route_page(route):
            path = route.request.url.rsplit("/", 1)[-1]
            if path == "fixture-login":
                return route.fulfill(json={"ready": logged_in.is_set()})
            if path in ("animation.gif", "still.png"):
                data = animation if path.endswith("gif") else fixture_image(False)
                return route.fulfill(body=data, headers={"Content-Type": "application/octet-stream", "Content-Disposition": "attachment"})
            login_button = "" if logged_in.is_set() else "<button id='fixture-sign-in'>Log in</button>"
            route.fulfill(content_type="text/html", body=login_button + '''
                <textarea id="prompt-textarea"></textarea>
                <button data-testid="send-button">Send</button>
                <article data-message-author-role="assistant" hidden>
                    <a href="/still.png">Download image</a>
                    <img alt="Animation" width="40" height="40">
                </article>
                <dialog><a href="/animation.gif">Download GIF</a></dialog>
                <script>
                    setInterval(async () => {
                        const state = await (await fetch('/fixture-login')).json();
                        if (state.ready) document.querySelector('#fixture-sign-in')?.remove();
                    }, 50);
                    document.querySelector('[data-testid="send-button"]').onclick = () => {
                        document.querySelector('article').hidden = false;
                    };
                    document.querySelector('img').onclick = () => document.querySelector('dialog').showModal();
                </script>
            ''')

        @contextmanager
        def routed_playwright():
            with sync_playwright() as playwright:
                launch = playwright.chromium.launch_persistent_context

                def routed_launch(*args, **kwargs):
                    launched.append(kwargs["headless"])
                    kwargs["headless"] = True
                    context = launch(*args, **kwargs)
                    context.route("https://chatgpt.com/**", route_page)
                    return context

                playwright.chromium.launch_persistent_context = routed_launch
                yield playwright

        with tempfile.TemporaryDirectory() as directory, patch("browser_worker.sync_playwright", routed_playwright), patch("browser_worker.launch_visible_chrome", lambda pw, profile: pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False, accept_downloads=True)):
            jobs = Jobs()
            browser = BrowserWorker(Path(directory) / "browser", jobs, animated_gif)
            server = ThreadingHTTPServer(("127.0.0.1", 0), http_handler(jobs, "test-token", browser))
            listener = threading.Thread(target=server.serve_forever)
            listener.start()
            browser.thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def request(path, data=None, method=None, auth=True):
                headers = {"Content-Type": "application/json"}
                if auth:
                    headers["Authorization"] = "Bearer test-token"
                return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers, data=data, method=method), timeout=5)

            def wait_state(status):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if browser.snapshot()["status"] == status:
                        return
                    time.sleep(.05)
                self.fail(f"Expected {status}, got {browser.snapshot()}")

            try:
                with request("/", auth=False) as response:
                    self.assertIn("GIF 管理", response.read().decode())
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    request("/health", auth=False)
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()
                wait_state("login_required")
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    request("/v1/gif/jobs", b'{"prompt":"Test motion"}')
                self.assertEqual(denied.exception.code, 409)
                denied.exception.close()
                logged_in.set()
                with request("/session/login", method="POST") as response:
                    self.assertEqual(response.status, 202)
                wait_state("signing_in")
                with request("/session/finish", method="POST") as response:
                    self.assertEqual(response.status, 202)
                wait_state("ready")
                with sync_playwright() as playwright:
                    dashboard_browser = playwright.chromium.launch(channel="chrome")
                    try:
                        page = dashboard_browser.new_page(viewport={"width": 1280, "height": 900})
                        page.goto(base)
                        page.locator("#token").fill("test-token")
                        page.locator('#login-form button').click()
                        page.locator('#account-status').get_by_text('已连接', exact=True).wait_for()
                        self.assertEqual(page.locator('#api-base').input_value(), base + '/v1')
                        self.assertEqual(page.locator('#api-model').input_value(), 'chatgpt-native-gif')
                        self.assertEqual(page.locator('#api-key').input_value(), 'test-token')
                        self.assertEqual(page.locator('#api-key').get_attribute('type'), 'password')
                        page.locator('#prompt').fill('Test motion')
                        page.locator('#submit-job').click()
                        page.get_by_text('已完成', exact=True).wait_for(timeout=20_000)
                        page.locator('.preview').wait_for(state='visible')
                        with page.expect_download() as pending:
                            page.get_by_role('button', name='下载 GIF', exact=True).click()
                        self.assertEqual(Path(pending.value.path()).read_bytes(), animation)
                        self.assertNotIn('让想法动起来', page.locator('body').inner_text())
                        self.assertTrue(page.locator('body').evaluate('(el) => el.scrollWidth <= innerWidth'))
                    finally:
                        dashboard_browser.close()
                job_id = jobs.list()[0]['id']
                path = "/v1/gif/jobs/" + job_id
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and jobs.get(job_id).status not in ("completed", "failed"):
                    time.sleep(.05)
                self.assertEqual(jobs.get(job_id).status, "completed", jobs.get(job_id).error)
                with request(path + "/result") as result:
                    self.assertEqual(result.headers.get_content_type(), "image/gif")
                    self.assertEqual(result.read(), animation)
                with request(path, method="DELETE") as response:
                    self.assertEqual(response.status, 200)
                with request("/v1/gif/jobs") as response:
                    self.assertEqual(json.load(response)["jobs"], [])
                self.assertEqual(launched, [False])
                self.assertFalse(browser.snapshot()['headless'])
            finally:
                browser.close()
                server.shutdown()
                listener.join()
                server.server_close()

    def test_static_image_is_rejected(self):
        with self.assertRaises(JobError):
            animated_gif(fixture_image(False))

    def test_current_web_composer_and_native_gif_viewer(self):
        animation = fixture_image()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome")
            context = browser.new_context(accept_downloads=True)
            def route_page(route):
                if route.request.url.endswith('/animation.gif'):
                    return route.fulfill(body=animation, headers={'Content-Disposition':'attachment', 'Content-Type':'image/gif'})
                route.fulfill(content_type='text/html; charset=utf-8', body='''
                    <div contenteditable="true" role="textbox" aria-label="询问 ChatGPT"></div>
                    <button aria-label="发送" onclick="document.querySelector('#expand').hidden=false">Send</button>
                    <button id="expand" aria-label="展开此 GIF" hidden onclick="document.querySelector('#download').hidden=false">Expand</button>
                    <a id="download" href="/animation.gif" hidden>下载 GIF</a>
                ''')
            context.route('https://chatgpt.com/**',route_page)
            try:
                self.assertEqual(ChatGPTBrowser(context,animated_gif,wait_seconds=10).generate('Test native GIF'),animation)
            finally:
                browser.close()

    def test_generation_timeout_does_not_report_success(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome")
            context = browser.new_context()
            context.route("https://chatgpt.com/**", lambda route: route.fulfill(content_type="text/html", body='<textarea id="prompt-textarea"></textarea><button data-testid="send-button">Send</button>'))
            try:
                from browser_worker import BrowserError
                with self.assertRaises(BrowserError):
                    ChatGPTBrowser(context, animated_gif, wait_seconds=.1).generate("Test")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
