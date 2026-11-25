"""Launch installed Chrome normally and attach through loopback CDP."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request


def chrome_executable():
    if sys.platform == 'darwin':
        candidates = [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'), Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    elif sys.platform == 'win32':
        candidates = [Path(os.environ.get(name, '')) / 'Google/Chrome/Application/chrome.exe' for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA') if os.environ.get(name)]
    else:
        candidates = [Path(path) for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser') if (path := shutil.which(name))]
    return next((str(path) for path in candidates if path.is_file()), None)


def launch_visible_chrome(playwright, profile):
    executable = chrome_executable()
    if not executable:
        raise RuntimeError('Installed Chrome was not found')
    profile = Path(profile).resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A stale marker must not attach this service to an unrelated browser.
    marker = profile / 'DevToolsActivePort'
    marker.unlink(missing_ok=True)
    process = subprocess.Popen([
        executable, '--user-data-dir=' + str(profile), '--remote-debugging-address=127.0.0.1',
        '--remote-debugging-port=0', '--no-first-run', 'https://chatgpt.com/',
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Chrome exited before the connection was ready')
            try:
                port = int(marker.read_text().splitlines()[0])
                if not 0 < port < 65536:
                    raise ValueError('Invalid port')
                endpoint = f'http://127.0.0.1:{port}'
                # Do not route local browser control through environment proxies.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(endpoint + '/json/version', timeout=1) as response:
                    json.load(response)
                browser = playwright.chromium.connect_over_cdp(endpoint, timeout=10_000)
                context = browser.contexts[0]
                context._bridge_process = process
                return context
            except (OSError, ValueError, IndexError):
                time.sleep(.1)
        raise RuntimeError('Timed out waiting for Chrome')
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise


def close_visible_chrome(context):
    process = getattr(context, '_bridge_process', None)
    try:
        context.close()
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
