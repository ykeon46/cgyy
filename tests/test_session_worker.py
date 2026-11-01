"""常驻浏览器进程：命令往返、预约任务生命周期、意外退出时的状态，以及会话复用。"""
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import MagicMock, patch

import web_app
import web_worker
from browser_session import BrowserSession

# 只实现协议的假进程：login 立即回复；book 先确认，收到 finish 文件后报告结束；crash 直接退出。
FAKE = textwrap.dedent('''
    import json, sys, time, pathlib
    for line in sys.stdin:
        command = json.loads(line)
        if command['op'] == 'crash':
            sys.exit(3)
        if command['op'] == 'book':
            print(json.dumps({'id': command['id'], 'ok': True}), flush=True)
            flag = pathlib.Path(command['directory']) / 'finish'
            while not flag.exists():
                time.sleep(0.02)
            print(json.dumps({'event': 'finished', 'task': command['task']}), flush=True)
            continue
        print(json.dumps({'id': command['id'], 'ok': command.get('username') != 'bad',
                          'username': command.get('username'), 'buddies': [], 'message': '密码错误'}), flush=True)
''')


class SessionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script = self.root / 'fake_worker.py'
        script.write_text(FAKE)
        for item in [patch('web_app.worker_command', return_value=([sys.executable, str(script)], {})),
                     patch('web_app.DATA', self.root)]:
            item.start();self.addCleanup(item.stop)
        self.worker = web_app.SessionWorker()
        self.addCleanup(lambda: self.worker.process and self.worker.process.kill())

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline:
                self.fail('等待超时')
            time.sleep(0.02)

    def test_commands_reuse_one_process(self):
        self.assertTrue(self.worker.call('login', {'username': 'a', 'password': 'p'})['ok'])
        first = self.worker.process.pid
        self.assertFalse(self.worker.call('login', {'username': 'bad', 'password': 'p'})['ok'])
        self.assertEqual(self.worker.process.pid, first)

    def test_booking_task_reports_running_until_finished_and_blocks_other_commands(self):
        directory = self.root / 'job'; directory.mkdir()
        task = self.worker.book({'directory': str(directory), 'mode': 'now'})
        self.assertIsNone(task.poll())
        self.assertIsNone(self.worker.call('login', {'username': 'a', 'password': 'p'}))
        self.assertIsNone(self.worker.book({'directory': str(directory)}))
        (directory / 'finish').write_text('1')
        self.wait(lambda: task.poll() == 0)
        self.assertTrue(self.worker.call('login', {'username': 'a', 'password': 'p'})['ok'])

    def test_crash_marks_unsubmitted_task_failed_and_submitted_task_for_review(self):
        for status, trade, expected in [('checking', None, 'failed'), ('submitting', None, 'needs_attention'),
                                        ('awaiting_payment', 'T1', 'qr_needs_attention')]:
            with self.subTest(status=status):
                directory = self.root / status; directory.mkdir()
                (directory / 'status.json').write_text(json.dumps({'status': status, 'trade_no': trade}))
                task = self.worker.book({'directory': str(directory)})
                self.worker.process.kill()
                self.wait(lambda: task.poll() is not None)
                self.assertEqual(json.loads((directory / 'status.json').read_text())['status'], expected)

    def test_spawn_worker_falls_back_to_one_off_process_while_busy(self):
        busy = MagicMock();busy.call.return_value = None
        with patch('web_app.SESSION_WORKER', busy), patch('web_app.local_environment', return_value={}), \
                patch('web_app.subprocess.run') as one_off:
            one_off.return_value.stdout = json.dumps({'ok': True, 'username': 'a', 'buddies': []})
            self.assertEqual(web_app.spawn_worker('login', {'username': 'a', 'password': 'p'})['username'], 'a')
        one_off.assert_called_once()

    def test_persistent_browser_can_be_disabled(self):
        with patch('web_app.SESSION_WORKER') as worker, patch('web_app.local_environment',
                return_value={'CGYY_PERSISTENT_BROWSER': '0'}), patch('web_app.subprocess.run') as one_off:
            one_off.return_value.stdout = json.dumps({'ok': True, 'username': 'a', 'buddies': []})
            web_app.spawn_worker('login', {'username': 'a', 'password': 'p'})
        worker.call.assert_not_called()


class BrowserSessionTests(unittest.TestCase):
    def test_recent_session_is_reused_without_any_request(self):
        session = BrowserSession(MagicMock())
        client = MagicMock()
        session.adopt('user', MagicMock(), client)
        with patch('browser_session.authenticated_client') as login:
            self.assertIs(session.acquire('user', 'password', config={'venue': 'x'})[1], client)
        client.check_session.assert_not_called()
        login.assert_not_called()
        self.assertEqual(client.config, {'venue': 'x'})

    def test_old_session_is_checked_once_and_relogged_only_when_invalid(self):
        session = BrowserSession(MagicMock())
        client, context = MagicMock(), MagicMock()
        session.adopt('user', context, client)
        session.verified_at = 0
        client.check_session.return_value = True
        with patch('browser_session.authenticated_client') as login:
            session.acquire('user', 'password')
            login.assert_not_called()
            session.mark_invalid()
            login.return_value = (MagicMock(), MagicMock())
            session.acquire('user', 'password')
        login.assert_called_once()
        context.close.assert_called_once()

    def test_other_account_never_reuses_session(self):
        session = BrowserSession(MagicMock())
        session.adopt('user-a', MagicMock(), MagicMock())
        with patch('browser_session.authenticated_client', return_value=(MagicMock(), MagicMock())) as login:
            session.acquire('user-b', 'password')
        login.assert_called_once()


class WorkerLoginTests(unittest.TestCase):
    def setUp(self):
        for item in [patch('web_worker.save_credentials'), patch('web_worker.LoginState')]:
            item.start();self.addCleanup(item.stop)

    def test_live_session_login_needs_only_the_buddy_request(self):
        session = BrowserSession(MagicMock())
        client = MagicMock();client.buddies.return_value = [{'index': 1, 'name': '同伴'}]
        session.adopt('user', MagicMock(), client)
        with patch.object(session, 'acquire') as acquire:
            answer = web_worker.login(session, {'username': 'user', 'password': 'pw'},
                                      {'BUAA_SSO_USER': 'user', 'BUAA_SSO_PASSWORD': 'pw'})
        self.assertEqual(answer['buddies'], [{'index': 1, 'name': '同伴'}])
        acquire.assert_not_called()
        client.check_session.assert_not_called()

    def test_new_password_always_goes_through_school_login(self):
        session = BrowserSession(MagicMock())
        old = MagicMock();session.adopt('user', MagicMock(), old)
        with patch('web_worker.VenueClient') as venue:
            venue.return_value.buddies.return_value = []
            web_worker.login(session, {'username': 'user', 'password': 'new'},
                             {'BUAA_SSO_USER': 'user', 'BUAA_SSO_PASSWORD': 'old'})
        venue.return_value.login.assert_called_once_with('user', 'new', ignore_password_warning=True)
        old.buddies.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class WarmTabTests(unittest.TestCase):
    VENUES = ['主馆', '副馆']

    def session_with_tabs(self):
        session = BrowserSession(MagicMock())
        base = MagicMock()
        session.adopt('user', MagicMock(), base)
        tabs = {'主馆': base, '副馆': MagicMock()}
        session.tabs = dict(tabs)
        return session, base, tabs

    def test_matching_tabs_are_handed_over_and_returned(self):
        session, base, tabs = self.session_with_tabs()
        with patch('browser_session.on_reservation_page', return_value=True):
            self.assertEqual(session.take_tabs(self.VENUES), tabs)
            self.assertEqual(session.tabs, {})
            session.keep_tabs(tabs)
        self.assertEqual(session.tabs, tabs)
        tabs['副馆'].page.close.assert_not_called()

    def test_stale_or_mismatched_tabs_are_closed_never_reused(self):
        for venues, valid in [(['副馆', '主馆'], True), (self.VENUES, False)]:
            with self.subTest(venues=venues, valid=valid):
                session, base, tabs = self.session_with_tabs()
                with patch('browser_session.on_reservation_page', return_value=valid):
                    self.assertEqual(session.take_tabs(venues), {})
                tabs['副馆'].page.close.assert_called_once()
                base.page.close.assert_not_called()
                self.assertEqual(session.tabs, {})

    def test_warm_tabs_skips_maintenance_hours_and_other_accounts(self):
        session = BrowserSession(MagicMock())
        session.adopt('user', MagicMock(), MagicMock())
        night = web_worker.datetime(2026, 11, 1, 23, 30, tzinfo=web_worker.CHINA)
        noon = web_worker.datetime(2026, 11, 1, 12, tzinfo=web_worker.CHINA)
        with patch.object(session, 'warm_up') as warm, patch('web_worker.datetime') as clock:
            clock.now.return_value = night
            web_worker.warm_tabs(session, {})
            clock.now.return_value = noon
            with patch('web_worker.load_config', return_value={'account_username': 'other', 'venues': ['x']}):
                web_worker.warm_tabs(session, {})
            web_worker.warm_tabs(session, {'CGYY_WARM_TABS': '0'})
        warm.assert_not_called()
