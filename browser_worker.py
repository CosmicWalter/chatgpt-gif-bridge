"""Serialized ChatGPT UI operations in a persistent Chrome profile."""

import queue
from collections import deque
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
COMPOSER = '#prompt-textarea, [contenteditable="true"][role="textbox"]'
SEND = re.compile(r"^send( message)?$|^发送$", re.I)
STOP = re.compile(r"^stop( generating)?$|^停止$", re.I)
EXPAND_GIF = re.compile(r"展开此 GIF|expand.*GIF|open.*GIF", re.I)


class BrowserError(Exception):
    """A browser failure that can be displayed without private browser data."""
    def __init__(self, message, code="browser_unavailable"):
        super().__init__(message)
        self.code = code


class VerificationRequired(BrowserError):
    def __init__(self):
        super().__init__("ChatGPT 需要真人验证，请打开验证窗口完成验证。", "verification_required")


class LoginRequired(BrowserError):
    def __init__(self):
        super().__init__("ChatGPT 登录已失效，请打开登录窗口重新登录。", "login_required")


class ChatGPTBrowser:
    def __init__(self, context, validate, stopped=None, wait_seconds=240):
        self.context = context
        self.validate = validate
        self.stopped = stopped or threading.Event()
        self.wait_seconds = wait_seconds

    def check_challenge(self, page):
        if page.locator('iframe[src*="challenges.cloudflare.com"], #challenge-running, #cf-challenge-running').count() or re.search(r"just a moment|请稍候|稍等片刻", page.title(), re.I):
            raise VerificationRequired()

    def signed_in(self, page):
        self.check_challenge(page)
        try:
            page.locator(COMPOSER).first.wait_for(state="visible", timeout=15_000)
        except BrowserTimeout:
            self.check_challenge(page)
            return False
        if not page.url.startswith(CHATGPT):
            return False
        for role in ("button", "link"):
            if any(control.is_visible() for control in page.get_by_role(role, name=SIGN_IN).all()):
                return False
        return True

    def generate(self, prompt, progress=None, phase="queued", chat_url=None):
        progress = progress or (lambda **fields: None)
        page = self.context.new_page()
        completed = False
        try:
            resume = phase in ("sent", "submitting")
            if resume and not (chat_url and re.fullmatch(r"https://chatgpt.com/c/[A-Za-z0-9-]+", chat_url)):
                raise BrowserError("上次发送结果尚未确认，请检查原聊天；为避免重复生成，未重新提交。", "submission_uncertain")
            page.goto(chat_url if resume else CHATGPT, wait_until="domcontentloaded", timeout=30_000)
            if not self.signed_in(page):
                raise LoginRequired()
            if not resume:
                page.locator(COMPOSER).first.fill(
                    "Generate a native animated GIF with a downloadable GIF file. "
                    "Use the built-in animation/image generation feature. "
                    "Do not write code or assemble still frames. Request: " + prompt
                )
                progress(phase="submitting")
                page.get_by_test_id("send-button").or_(page.get_by_role("button", name=SEND)).first.click(timeout=15_000)
                progress(phase="sent", chat_url=page.url if "/c/" in page.url else None)
            def record_url():
                if re.fullmatch(r"https://chatgpt.com/c/[A-Za-z0-9-]+", page.url):
                    progress(phase="sent", chat_url=page.url)
            data = self.wait_for_gif(page, record_url)
            completed = True
            return data
        finally:
            # Keep interrupted pages available in the persistent browser until recovery.
            if completed:
                page.close()

    def wait_for_gif(self, page, record_url=None):
        deadline = time.monotonic() + self.wait_seconds
        tried = set()
        while time.monotonic() < deadline:
            if record_url:
                record_url()
            self.check_challenge(page)
            if page.get_by_role("button", name=SIGN_IN).first.is_visible():
                raise LoginRequired()
            if self.stopped.is_set():
                raise BrowserError("服务已停止。")
            turns = page.locator('[data-message-author-role="assistant"]')
            stopping = page.get_by_test_id("stop-button").or_(page.get_by_role("button", name=STOP))
            if not stopping.first.is_visible():
                expand = page.get_by_role("button", name=EXPAND_GIF).first
                if expand.is_visible() and "native-viewer" not in tried:
                    tried.add("native-viewer")
                    expand.click(timeout=1_500)
                if "native-viewer" in tried:
                    result = self.download_from(page, page, "native-viewer", tried)
                    if result:
                        return result
            if turns.count() and not stopping.first.is_visible():
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
        self.headless = True
        self.commands = queue.Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def snapshot(self):
        with self.lock:
            return {"status": self.state, "error": self.error, "headless": self.headless, "mode": "headless"}

    def set_state(self, state, error=None):
        with self.lock:
            self.state, self.error = state, error

    def login(self):
        with self.lock:
            if self.state not in ("ready", "login_required", "verification_required", "error"):
                raise BrowserError("浏览器正在处理请求，请稍后重试。")
            if self.state == "ready" and any(job["status"] in ("queued", "running") for job in self.jobs.list()):
                raise BrowserError("请等待当前任务完成。")
            self.state, self.error = "opening_login", None
            self.commands.put(("login", None))

    def finish_login(self):
        with self.lock:
            if self.state != "signing_in":
                raise BrowserError("尚未打开登录或验证窗口。")
            self.state, self.error = "checking", None
            self.commands.put(("finish", None))

    def submit(self, prompt):
        with self.lock:
            if self.state not in ("ready", "busy"):
                raise BrowserError(self.error or "请先完成 ChatGPT 登录和验证。", self.state if self.state in ("login_required", "verification_required") else "browser_unavailable")
            job_id = self.jobs.submit(prompt)
            self.commands.put(("generate", job_id))
            return job_id

    def run(self):
        context = None
        pending = deque(row["id"] for row in reversed(self.jobs.list()) if row["status"] in ("queued", "running", "waiting_verification"))
        try:
            with sync_playwright() as playwright:
                def open_browser(headless):
                    result = playwright.chromium.launch_persistent_context(
                        str(self.profile), channel="chrome", headless=headless, accept_downloads=True,
                    )
                    with self.lock:
                        self.headless = headless
                    return result

                def check_login():
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(CHATGPT, wait_until="domcontentloaded", timeout=30_000)
                    if not ChatGPTBrowser(context, self.validate, self.stopped).signed_in(page):
                        raise LoginRequired()
                    self.set_state("ready")

                def record_error(error):
                    if isinstance(error, BrowserError):
                        state = error.code if error.code in ("verification_required", "login_required") else "error"
                        self.set_state(state, str(error))
                    else:
                        self.set_state("error", "浏览器或网络连接失败，请检查 Chrome 和网络后重新打开登录窗口。")

                try:
                    context = open_browser(True)
                    check_login()
                except Exception as error:
                    record_error(error)

                try:
                    while not self.stopped.is_set():
                        command, job_id = None, None
                        try:
                            command, job_id = self.commands.get(timeout=.1 if pending else .5)
                        except queue.Empty:
                            pass
                        if command == "stop":
                            break
                        if command == "generate":
                            pending.append(job_id)
                        if command in ("login", "finish"):
                            try:
                                if command == "finish":
                                    # Check the existing visible page without navigating or restarting it.
                                    page = next((page for page in context.pages if page.url.startswith(CHATGPT)), None) if context else None
                                    try:
                                        if page is None or not ChatGPTBrowser(context, self.validate, self.stopped).signed_in(page):
                                            raise LoginRequired()
                                    except (LoginRequired, VerificationRequired) as error:
                                        self.set_state("signing_in", str(error) + " 当前窗口已保留，请勿反复点击完成。")
                                        continue
                                if context:
                                    context.close()
                                    context = None
                                context = open_browser(command == "finish")
                                if command == "login":
                                    page = context.pages[0] if context.pages else context.new_page()
                                    # Reopen the interrupted original chat for manual inspection if available.
                                    interrupted = self.jobs.get(pending[0]) if pending else None
                                    url = interrupted.chat_url if interrupted and interrupted.chat_url else CHATGPT
                                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                                    self.set_state("signing_in")
                                else:
                                    check_login()
                            except Exception as error:
                                record_error(error)
                        if not pending or self.snapshot()["status"] != "ready":
                            continue
                        job_id = pending[0]
                        job = self.jobs.get(job_id)
                        if not job or job.status in ("completed", "failed"):
                            pending.popleft()
                            continue
                        self.set_state("busy")
                        self.jobs.update(job_id, status="running", error=None)
                        last_progress = {}
                        def progress(**fields):
                            if fields != last_progress:
                                self.jobs.update(job_id, **fields)
                                last_progress.clear()
                                last_progress.update(fields)
                        try:
                            data = ChatGPTBrowser(context, self.validate, self.stopped).generate(
                                job.prompt, progress, job.phase, job.chat_url,
                            )
                            self.jobs.update(job_id, status="completed", result=data, phase="completed")
                            pending.popleft()
                            self.set_state("ready")
                        except Exception as error:
                            if self.stopped.is_set():
                                break
                            if isinstance(error, (VerificationRequired, LoginRequired)):
                                self.jobs.update(job_id, status="waiting_verification", error=str(error))
                                record_error(error)
                            else:
                                message = str(error) if isinstance(error, BrowserError) else "浏览器操作中断，请核验原聊天结果；未自动重发。"
                                self.jobs.update(job_id, status="failed", error=message)
                                pending.popleft()
                                if isinstance(error, BrowserError) and error.code in ("browser_unavailable", "submission_uncertain"):
                                    self.set_state("ready")
                                else:
                                    record_error(error)
                finally:
                    if context:
                        context.close()
        except Exception:
            self.set_state("error", "浏览器进程启动失败。")
        finally:
            self.stopped.set()

    def close(self):
        self.stopped.set()
        self.commands.put(("stop", None))
        self.thread.join(timeout=40)
