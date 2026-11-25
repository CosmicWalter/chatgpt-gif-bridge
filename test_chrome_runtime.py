import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chrome_runtime import launch_visible_chrome, close_visible_chrome


class ChromeRuntimeTests(unittest.TestCase):
    def test_direct_launch_uses_loopback_without_automation_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            process = Mock()
            process.poll.return_value = None
            context = Mock()
            browser = SimpleNamespace(contexts=[context])
            connect = Mock(return_value=browser)
            playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
            def start(arguments, **options):
                (profile / 'DevToolsActivePort').write_text('54321\n/devtools/browser/fixture')
                return process
            @contextmanager
            def version_response(*args, **kwargs):
                import io
                yield io.BytesIO(json.dumps({'Browser':'Chrome'}).encode())
            opener = Mock()
            opener.open.side_effect = version_response
            with patch('chrome_runtime.chrome_executable', return_value='/test/chrome'), patch('chrome_runtime.subprocess.Popen', side_effect=start) as launch, patch('chrome_runtime.urllib.request.build_opener', return_value=opener):
                result = launch_visible_chrome(playwright, profile)
            arguments = launch.call_args.args[0]
            self.assertIn('--remote-debugging-address=127.0.0.1', arguments)
            self.assertIn('--remote-debugging-port=0', arguments)
            self.assertFalse(any('headless' in argument or 'enable-automation' in argument or 'mock-keychain' in argument or 'password-store' in argument or 'disable-blink' in argument for argument in arguments))
            self.assertIs(result, context)
            connect.assert_called_once_with('http://127.0.0.1:54321', timeout=10_000)
            close_visible_chrome(context)
            context.close.assert_called_once()
            process.terminate.assert_called_once()

    def test_startup_failure_cleans_up_only_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock()
            process.poll.return_value = 1
            with patch('chrome_runtime.chrome_executable', return_value='/test/chrome'), patch('chrome_runtime.subprocess.Popen', return_value=process):
                with self.assertRaises(RuntimeError):
                    launch_visible_chrome(Mock(), Path(directory))
            process.terminate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
