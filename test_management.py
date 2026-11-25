import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from playwright.sync_api import sync_playwright, expect
from bridge import Jobs, http_handler
from credentials import Credentials


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.keys = Credentials(self.path, 'legacy-api-key')
        self.admin = (self.path / 'management-key').read_text()
        self.jobs = Jobs()
        self.browser = SimpleNamespace(snapshot=lambda: {'status':'login_required','error':None,'headless':False,'mode':'visible_chrome'})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), http_handler(self.jobs, 'legacy-api-key', self.browser, credentials=self.keys))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.route = '/v8/management/config/access/api-keys'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()

    def request(self, path, key, body=None, method=None, alternate=False):
        headers = {'Content-Type':'application/json'}
        headers['X-Management-Key' if alternate else 'Authorization'] = key if alternate else 'Bearer ' + key
        request = urllib.request.Request(self.base + path, headers=headers, data=json.dumps(body).encode() if body is not None else None, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_permissions_and_legacy_key_migration(self):
        self.assertEqual(self.request('/v1/models', 'legacy-api-key')[0], 200)
        for path in (self.route, '/v8/management/status'):
            self.assertEqual(self.request(path, 'legacy-api-key')[0], 401)
        for path in ('/session', '/health'):
            self.assertEqual(self.request(path, 'legacy-api-key')[0], 200)
        for path in ('/session/login', '/session/finish'):
            self.assertEqual(self.request(path, 'legacy-api-key', {}, 'POST')[0], 401)
        self.assertEqual(self.request('/v1/models', self.admin)[0], 401)
        status, data = self.request(self.route, self.admin, alternate=True)
        self.assertEqual(status, 200)
        self.assertEqual(data['api-keys'], ['legacy-api-key'])
        self.assertNotIn(self.admin, json.dumps(data))
        self.assertNotIn(self.admin, (self.path / 'credentials.json').read_text())

    def test_key_mutations_apply_without_restart_and_survive_reload(self):
        status, data = self.request(self.route, self.admin, {}, 'POST')
        self.assertEqual(status, 201)
        generated = data['key']
        self.assertEqual(self.request('/v1/models', generated)[0], 200)
        self.assertEqual(self.request(self.route, self.admin, {'old':generated,'new':'custom-new-key'}, 'PATCH')[0], 200)
        self.assertEqual(self.request('/v1/models', generated)[0], 401)
        self.assertEqual(self.request('/v1/models', 'custom-new-key')[0], 200)
        self.assertEqual(self.request(self.route, self.admin, {'key':'custom-new-key'}, 'POST')[0], 400)
        self.assertEqual(self.request(self.route, self.admin, {'key':'custom-new-key'}, 'DELETE')[0], 200)
        self.assertEqual(self.request('/v1/models', 'custom-new-key')[0], 401)
        reloaded = Credentials(self.path, 'ignored-legacy-key')
        self.assertEqual(reloaded.keys(), ['legacy-api-key'])
        self.assertTrue(reloaded.is_management(self.admin))
        self.assertEqual(self.request(self.route, self.admin, {'key':'legacy-api-key'}, 'DELETE')[0], 200)
        self.assertEqual(self.request('/v1/models', 'legacy-api-key')[0], 401)
        self.assertEqual(self.request(self.route, self.admin)[0], 200)

    def test_management_rotation_revokes_only_management(self):
        path = '/v8/management/management-key'
        self.assertEqual(self.request(path, self.admin, {'current_key':'wrong','new_key':'new-management-secret'}, 'PUT')[0], 400)
        self.assertEqual(self.request(path, self.admin, {'current_key':self.admin,'new_key':'new-management-secret'}, 'PUT')[0], 200)
        self.assertEqual(self.request(self.route, self.admin)[0], 401)
        self.assertEqual(self.request(self.route, 'new-management-secret')[0], 200)
        self.assertEqual(self.request('/v1/models', 'legacy-api-key')[0], 200)
        self.assertFalse((self.path / 'management-key').exists())
        reloaded = Credentials(self.path, 'ignored', reset_management=True)
        replacement = (self.path / 'management-key').read_text()
        self.assertTrue(reloaded.is_management(replacement))
        self.assertFalse(reloaded.is_management('new-management-secret'))
        self.assertEqual(reloaded.keys(), ['legacy-api-key'])

    def test_management_ui_key_crud_remember_and_logout(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome')
            context = browser.new_context(viewport={'width':1280,'height':900}, permissions=['clipboard-read','clipboard-write'])
            page = context.new_page()
            try:
                page.goto(self.base + '/management.html')
                page.locator('#token').fill('legacy-api-key')
                page.locator('#login-form button[type=submit]').click()
                page.locator('#login-error').get_by_text('管理密钥不正确。', exact=True).wait_for()
                page.locator('#token').fill(self.admin)
                page.locator('#remember-key').check()
                page.locator('#login-form button[type=submit]').click()
                page.locator('#workspace').wait_for(state='visible')
                page.locator('nav a[data-page=keys]').click()
                page.locator('.key-row').wait_for()
                self.assertEqual(page.locator('.key-row input').input_value(), 'legacy-api-key')
                self.assertEqual(page.locator('.key-row input').get_attribute('type'), 'password')
                page.get_by_role('button', name='生成密钥', exact=True).click()
                expect(page.locator('.key-row')).to_have_count(2)
                page.locator('.key-row').first.get_by_role('button', name='显示', exact=True).click()
                self.assertEqual(page.locator('.key-row input').first.get_attribute('type'), 'text')
                page.locator('.key-row').first.get_by_role('button', name='复制', exact=True).click()
                self.assertEqual(page.evaluate('navigator.clipboard.readText()'), 'legacy-api-key')
                page.locator('.key-row').first.get_by_role('button', name='编辑', exact=True).click()
                page.locator('#edit-api-key').fill('edited-client-key')
                page.locator('#edit-key-form button[type=submit]').click()
                page.locator('#edit-key-dialog').wait_for(state='hidden')
                self.assertFalse(self.keys.is_api('legacy-api-key'))
                self.assertTrue(self.keys.is_api('edited-client-key'))
                page.locator('#new-api-key').fill('custom-client-key')
                page.locator('#add-key-form button').click()
                expect(page.locator('.key-row')).to_have_count(3)
                page.locator('.key-row').first.get_by_role('button', name='删除', exact=True).click()
                page.locator('#confirm-delete-key').click()
                expect(page.locator('.key-row')).to_have_count(2)
                self.assertFalse(self.keys.is_api('edited-client-key'))
                page.screenshot(path='/tmp/bridge-cpa-management.png')
                self.assertTrue(page.locator('body').evaluate('(el) => el.scrollWidth <= innerWidth'), page.evaluate('() => ({width:innerWidth,scroll:document.body.scrollWidth,offenders:[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right}))})'))
                page.reload()
                page.locator('#workspace').wait_for(state='visible')
                page.locator('#disconnect').click()
                self.assertIsNone(page.evaluate('localStorage.getItem("bridge-management-key")'))
                self.assertIsNone(page.evaluate('sessionStorage.getItem("bridge-management-key")'))
                page.locator('#login').wait_for(state='visible')
                page.set_viewport_size({'width':390,'height':844})
                self.assertTrue(page.locator('body').evaluate('(el) => el.scrollWidth <= innerWidth'), page.evaluate('() => ({width:innerWidth,scroll:document.body.scrollWidth,offenders:[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right}))})'))
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
