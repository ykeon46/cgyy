"""登录态跨上下文复用、失效后回退和持久化范围。"""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from cgyy import DEFAULT_CONFIG, VenueClient, authenticated_client, load_config
from login_state import LoginState


class LoginStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'auth'/'school-session.json'
        self.cache = LoginState(self.path)
        self.state = {
            'cookies': [dict(name='ticket',value='test-cookie',domain='.buaa.edu.cn',path='/',expires=-1,httpOnly=True,secure=True,sameSite='Lax'),
                        dict(name='payment',value='must-not-persist',domain='.alipay.com',path='/',expires=-1,httpOnly=True,secure=True,sameSite='Lax')],
            'origins': [dict(origin='https://cgyy.buaa.edu.cn',localStorage=[dict(name='dataSix',value='test-token')]),
                        dict(origin='https://cashier.alipay.com',localStorage=[dict(name='account',value='must-not-persist')])],
        }

    def tearDown(self):
        self.directory.cleanup()

    def test_persists_school_only_in_private_file_and_binds_account(self):
        context=Mock();context.storage_state.return_value=self.state
        self.cache.save(context,'test-user')
        value=json.loads(self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),0o600)
        self.assertNotIn('must-not-persist',self.path.read_text())
        self.assertNotIn('test-user',self.path.read_text())
        self.assertEqual(len(value['storage_state']['cookies']),1)
        browser=Mock()
        self.cache.new_context(browser,'test-user')
        self.assertTrue(self.cache.loaded)
        self.assertEqual(browser.new_context.call_args.kwargs['storage_state'],value['storage_state'])
        self.cache.new_context(browser,'different-user')
        self.assertFalse(self.cache.loaded)
        self.assertNotIn('storage_state',browser.new_context.call_args.kwargs)

    def test_invalid_cache_and_disabled_cache_start_fresh(self):
        self.path.parent.mkdir();self.path.write_text('{broken-json')
        browser=Mock()
        self.cache.new_context(browser,'test-user')
        self.assertFalse(self.cache.loaded)
        self.assertNotIn('storage_state',browser.new_context.call_args.kwargs)
        cache=LoginState(None)
        cache.save(Mock(),'test-user')
        cache.new_context(browser,'test-user')
        self.assertFalse(cache.loaded)

    def test_valid_session_does_not_request_password_or_log_in_again(self):
        cache=Mock();cache.loaded=True
        context=cache.new_context.return_value
        password_loader=Mock(side_effect=AssertionError('password must not be requested'))
        with patch('cgyy.VenueClient') as factory:
            factory.return_value.resume_login.return_value=True
            actual,client=authenticated_client(Mock(),{},'test-user',password_loader,cache)
        self.assertIs(actual,context)
        client.login.assert_not_called()
        cache.save.assert_called_once_with(context,'test-user')

    def test_expired_session_uses_clean_context_before_password_login(self):
        cache=Mock();cache.loaded=True
        old,clean=Mock(),Mock();cache.new_context.side_effect=[old,clean]
        password_loader=Mock(return_value='test-password')
        with patch('cgyy.VenueClient') as factory:
            first,second=Mock(),Mock();first.resume_login.return_value=False
            factory.side_effect=[first,second]
            context,client=authenticated_client(Mock(),{},'test-user',password_loader,cache)
        old.close.assert_called_once()
        self.assertIs(context,clean)
        self.assertIs(client,second)
        self.assertEqual(cache.new_context.call_args.kwargs,{'restore':False})
        second.login.assert_called_once_with('test-user','test-password',False)


class LoginStateBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        options={'headless':True}
        if os.getenv('CGYY_BROWSER_BINARY'): options['executable_path']=os.environ['CGYY_BROWSER_BINARY']
        cls.browser=cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()

    def test_round_trip_restores_cookie_and_local_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=LoginState(Path(directory)/'state.json')
            first=self.browser.new_context()
            first.add_cookies([dict(name='ticket',value='test-cookie',domain='cgyy.buaa.edu.cn',path='/')])
            page=first.new_page();page.route('**/*',lambda route:route.fulfill(body='test'))
            page.goto('https://cgyy.buaa.edu.cn/')
            page.evaluate("localStorage.setItem('dataSix','test-token')")
            cache.save(first,'test-user');first.close()
            second=cache.new_context(self.browser,'test-user')
            try:
                page=second.new_page();page.route('**/*',lambda route:route.fulfill(body='test'))
                page.goto('https://cgyy.buaa.edu.cn/')
                self.assertEqual(page.evaluate("localStorage.getItem('dataSix')"),'test-token')
                self.assertEqual(second.cookies()[0]['value'],'test-cookie')
            finally:second.close()

    def test_server_rejection_is_detected_despite_cached_logout_link(self):
        context=self.browser.new_context()
        try:
            page=context.new_page()
            page.route('**/*',lambda route:route.fulfill(content_type='text/html',body='''<meta charset="utf-8">
                <a>退出</a><div id="app"></div><script>
                document.querySelector('#app').__vue__={userInfo:{username:'test-user'},
                    commonMethods:{emitAjax:options=>options.error({code:401})}};
                </script>'''))
            client=VenueClient(page,load_config(DEFAULT_CONFIG))
            self.assertFalse(client.resume_login('test-user'))
        finally:context.close()
