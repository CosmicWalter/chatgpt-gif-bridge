"""Serialized ChatGPT UI operations in a persistent Chrome profile."""

import queue
import re
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

CHATGPT = "https://chatgpt.com/"
DOWNLOAD = re.compile(r"^(download|save)( gif| image| animation)?$|下载|保存", re.I)
SIGN_IN = re.compile(r"^log in$|^sign in$|^登录$|^登入$", re.I)


class BrowserError(Exception):
    """A browser failure that can be displayed without private browser data."""


class ChatGPTBrowser:
    def __init__(self, context, validate, stopped=None, wait_seconds=240):
        self.context = context
        self.validate = validate
        self.stopped = stopped or threading.Event()
        self.wait_seconds = wait_seconds

    def signed_in(self, page):
        try:
            page.locator("#prompt-textarea").wait_for(state="visible", timeout=15_000)
        except BrowserTimeout:
            return False
        if not page.url.startswith(CHATGPT):
            return False
        for role in ("button", "link"):
            if any(control.is_visible() for control in page.get_by_role(role, name=SIGN_IN).all()):
                return False
        return True

    def generate(self, prompt):
        page = self.context.new_page()
        try:
            page.goto(CHATGPT, wait_until="domcontentloaded", timeout=30_000)
            if not self.signed_in(page):
                raise BrowserError("ChatGPT 登录已失效或页面不可用，请重新登录。")
            page.locator("#prompt-textarea").fill(
                "Generate a native animated GIF with a downloadable GIF file. "
                "Use the built-in animation/image generation feature. "
                "Do not write code or assemble still frames. Request: " + prompt
            )
            page.get_by_test_id("send-button").click(timeout=15_000)
            return self.wait_for_gif(page)
        finally:
            page.close()

    def wait_for_gif(self, page):
        deadline = time.monotonic() + self.wait_seconds
        tried = set()
        while time.monotonic() < deadline:
            if self.stopped.is_set():
                raise BrowserError("服务已停止。")
            turns = page.locator('[data-message-author-role="assistant"]')
            if turns.count() and not page.get_by_test_id("stop-button").is_visible():
                turn = turns.last
                try:
                    turn.hover(timeout=1_500)
                except BrowserTimeout:
                    pass
                result = self.download_from(turn, page, "turn", tried)
                if result:
                    return result
                for index, preview in enumerate(turn.locator("img, video").all()):
                    marker = ("preview", index, preview.get_attribute("src"))
                    if marker in tried or not preview.is_visible():
                        continue
                    tried.add(marker)
                    try:
                        preview.click(timeout=1_500)
                        dialog = page.get_by_role("dialog").last
                        if dialog.is_visible():
                            result = self.download_from(dialog, page, marker, tried)
                            if result:
                                return result
                    except BrowserTimeout:
                        pass
                    finally:
                        page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        raise BrowserError("未取得多帧 GIF。请确认该账号网页端支持 GIF，或检查网页是否有提示。")

    def download_from(self, container, page, scope, tried):
        controls = container.get_by_role("button", name=DOWNLOAD).or_(
            container.get_by_role("link", name=DOWNLOAD)
        )
        for index, control in enumerate(controls.all()):
            if not control.is_visible():
                continue
            marker = (scope, index, control.get_attribute("href"))
            if marker in tried:
                continue
            tried.add(marker)
            try:
                with page.expect_download(timeout=2_500) as pending:
                    control.click(timeout=1_500)
                download = pending.value
                try:
                    with TemporaryDirectory() as directory:
                        target = Path(directory) / "download"
                        download.save_as(target)
                        if target.stat().st_size > 25 * 1024 * 1024:
                            raise BrowserError("生成文件超过 25 MiB。")
                        try:
                            return self.validate(target.read_bytes())
                        except ValueError:
                            continue
                finally:
                    download.delete()
            except BrowserTimeout:
                continue
        return None


class BrowserWorker:
    def __init__(self, profile, jobs, validate):
        self.profile = profile
        self.jobs = jobs
        self.validate = validate
        self.lock = threading.Lock()
        self.state = "starting"
        self.error = None
        self.commands = queue.Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def snapshot(self):
        with self.lock:
            return {"status": self.state, "error": self.error, "headless": self.state != "signing_in"}

    def set_state(self, state, error=None):
        with self.lock:
            self.state, self.error = state, error

    def login(self):
        with self.lock:
            if self.state not in ("ready", "login_required", "error"):
                raise BrowserError("浏览器正在处理请求，请稍后重试。")
            if any(job["status"] in ("queued", "running") for job in self.jobs.list()):
                raise BrowserError("请等待当前任务完成。")
            self.state, self.error = "opening_login", None
            self.commands.put(("login", None))

    def finish_login(self):
        with self.lock:
            if self.state != "signing_in":
                raise BrowserError("尚未打开登录窗口。")
            self.state, self.error = "checking", None
            self.commands.put(("finish", None))

    def submit(self, prompt):
        with self.lock:
            if self.state not in ("ready", "busy"):
                raise BrowserError("请先登录 ChatGPT。")
            job_id = self.jobs.submit(prompt)
            self.commands.put(("generate", job_id))
            return job_id

    def run(self):
        context = None
        try:
            with sync_playwright() as playwright:
                def open_browser(headless):
                    return playwright.chromium.launch_persistent_context(
                        str(self.profile), channel="chrome", headless=headless, accept_downloads=True,
                    )

                def check_login():
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(CHATGPT, wait_until="domcontentloaded", timeout=30_000)
                    browser = ChatGPTBrowser(context, self.validate, self.stopped)
                    self.set_state("ready" if browser.signed_in(page) else "login_required")

                try:
                    context = open_browser(True)
                    check_login()
                except Exception:
                    self.set_state("error", "浏览器初始化失败，请检查 Chrome 和网络连接。")

                while not self.stopped.is_set():
                    try:
                        command, job_id = self.commands.get(timeout=.5)
                    except queue.Empty:
                        continue
                    if command == "stop":
                        break
                    try:
                        if command in ("login", "finish"):
                            if context:
                                context.close()
                                context = None
                            context = open_browser(command == "finish")
                            if command == "login":
                                page = context.pages[0] if context.pages else context.new_page()
                                page.goto(CHATGPT, wait_until="domcontentloaded", timeout=30_000)
                                self.set_state("signing_in")
                            else:
                                check_login()
                        elif command == "generate":
                            self.set_state("busy")
                            self.jobs.update(job_id, status="running")
                            prompt = self.jobs.get(job_id).prompt
                            data = ChatGPTBrowser(context, self.validate, self.stopped).generate(prompt)
                            self.jobs.update(job_id, status="completed", result=data)
                            self.set_state("ready")
                    except Exception as error:
                        message = str(error) if isinstance(error, BrowserError) else "浏览器操作失败，请检查登录状态和网络连接。"
                        if command == "generate":
                            self.jobs.update(job_id, status="failed", error=message)
                        self.set_state("error", message)
                if context:
                    context.close()
                    context = None
        except Exception:
            self.set_state("error", "浏览器进程启动失败。")
        finally:
            self.stopped.set()

    def close(self):
        self.stopped.set()
        self.commands.put(("stop", None))
        self.thread.join(timeout=40)
