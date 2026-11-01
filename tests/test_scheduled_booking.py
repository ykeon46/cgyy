from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from playwright.sync_api import TimeoutError as BrowserTimeout
import sys

if sys.platform not in {'linux', 'darwin'}:
    raise unittest.SkipTest('预约任务需要 POSIX 文件锁')

from cgyy import CHINA, DEFAULT_CONFIG, ReservationError, TaskStopped, load_config
from scheduled_booking import BookingJob, daily_config, main
from payment import PaymentError
from decimal import Decimal


class ScheduledBookingTests(unittest.TestCase):
    def setUp(self):
        # 测试中不向真实推送地址发送消息。
        silence = patch('scheduled_booking.notify.send');silence.start();self.addCleanup(silence.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = dict(load_config(DEFAULT_CONFIG), reserve_date='2026-10-21', reserve_time=[20,21],
            venues=['学院路校区-体育馆主馆-羽毛球','学院路校区-体育馆训练馆(副馆)-羽毛球'],
            start_at='2026-10-19T06:00:00+08:00', release_at='2026-10-19T07:00:00+08:00', stop_at='2026-10-19T08:00:00+08:00')
        with patch('scheduled_booking.target_date', return_value=date(2026,10,21)):
            self.job = BookingJob(self.config, self.root/'status.json')
        self.client = MagicMock()
        self.choice = [{'court':'1号','start':'20:00','end':'21:00'}, {'court':'1号','start':'21:00','end':'22:00'}]

    def tearDown(self):
        self.temp.cleanup()

    def test_account_mismatch_stops_before_login_or_submission(self):
        self.job.config['account_username']='account-a'
        with patch.object(self.job,'login') as login,patch('scheduled_booking.sleep_until') as wait:
            with self.assertRaisesRegex(ReservationError,'账号不一致'):
                self.job.run(MagicMock(),'account-b','dummy-password')
        login.assert_not_called()
        wait.assert_not_called()
        self.assertFalse(self.job.state_path.exists())

    def test_shahe_is_rejected(self):
        self.config['venues'].append('沙河校区-综合馆-羽毛球')
        with patch('scheduled_booking.target_date', return_value=date(2026,10,21)):
            with self.assertRaisesRegex(ReservationError, '排除沙河'):
                BookingJob(self.config, self.root/'status.json')

    def test_falls_back_to_training_hall_and_stops_after_one_order(self):
        self.client.submit.return_value = {'tradeNo':'example-trade'}
        with patch('scheduled_booking.available_choice', side_effect=[[],self.choice]):
            self.assertTrue(self.job.scan(self.client))
        self.assertEqual(self.client.open_venue.call_count, 2)
        self.client.submit.assert_called_once_with(self.choice)
        state = json.loads(self.job.state_path.read_text())
        self.assertEqual(state['status'], 'locked')
        self.assertEqual(state['venue'], self.config['venues'][1])
        with self.assertRaises(ReservationError):
            self.job.scan(self.client)
        self.assertEqual(self.client.submit.call_count, 1)

    def test_fast_scan_refreshes_prepared_venue_and_passes_captcha_budget(self):
        self.job.fast = True
        self.job.prepared = {self.config['venues'][0]}
        self.job.captcha_solver = object()
        self.client.find_choice.return_value = [dict(slot,classes=['free']) for slot in self.choice]
        self.client.submit.return_value = {'tradeNo':'fast-test-order'}
        self.assertTrue(self.job.scan(self.client))
        self.client.refresh_availability.assert_called_once()
        self.client.open_venue.assert_not_called()
        self.client.inspect.assert_not_called()
        options = self.client.submit.call_args.kwargs
        self.assertGreater(options['timeout'], 0)
        self.assertLessEqual(options['timeout'], 10000)
        self.assertIs(options['captcha_solver'], self.job.captcha_solver)
        state=json.loads(self.job.state_path.read_text())
        self.assertEqual(state['status'], 'locked')
        self.assertIn('booking_elapsed_seconds', state)

    def test_slow_initial_page_does_not_exhaust_fast_submission_budget(self):
        self.job.fast = True
        elapsed = [0.0]
        self.client.open_venue.side_effect = lambda: elapsed.__setitem__(0, 20.0)
        self.client.choose_date.side_effect = lambda date: elapsed.__setitem__(0, 25.0)
        self.client.find_choice.side_effect = lambda: (
            elapsed.__setitem__(0, 26.0) or [dict(slot, classes=['free']) for slot in self.choice])
        self.client.submit.return_value = {'tradeNo':'test-order'}
        with patch('scheduled_booking.time.monotonic', side_effect=lambda: elapsed[0]):
            self.assertTrue(self.job.scan(self.client))
        self.assertEqual(self.client.submit.call_args.kwargs['timeout'], 9000)

    def test_immediate_fast_scan_leaves_time_for_school_captcha_response(self):
        self.job.fast = True
        self.job.poll_availability = False
        self.client.find_choice.return_value = [dict(slot,classes=['free']) for slot in self.choice]
        self.client.submit.return_value = {'tradeNo':'test-order'}
        self.assertTrue(self.job.scan(self.client))
        timeout = self.client.submit.call_args.kwargs['timeout']
        self.assertGreater(timeout, 40000)
        self.assertLessEqual(timeout, 45000)

    def test_fast_job_prepares_and_waits_before_its_first_scan(self):
        self.job.fast = True
        context = MagicMock()
        with patch.object(self.job,'login',return_value=(context,self.client)) as login, \
                patch.object(self.job,'keep_session_until',return_value=(context,self.client)) as keep, \
                patch.object(self.job,'scan',return_value=True) as scan, \
                patch.object(self.job,'prepare_all',side_effect=lambda client,fresh:self.job.prepared.add('x')) as prepare, \
                patch.object(self.job,'begin_refreshes') as refresh, \
                patch('scheduled_booking.sleep_until') as wait, patch('scheduled_booking.datetime') as clock:
            clock.now.return_value=datetime(2026,10,19,6,tzinfo=CHINA)
            self.assertEqual(self.job.run(MagicMock(),'test-user','test-password'),0)
        login.assert_called_once()
        scan.assert_called_once_with(self.client)
        prepare.assert_called_once_with(self.client,fresh=False)
        refresh.assert_called_once()
        self.assertEqual(keep.call_count,2)
        self.assertEqual(wait.call_args.args[0],self.job.release)

    def test_job_after_release_prepares_all_venues_without_extra_refresh(self):
        self.job.fast=True
        self.job.poll_availability=False
        context=MagicMock()
        with patch.object(self.job,'login',return_value=(context,self.client)), \
                patch.object(self.job,'prepare_all') as prepare, \
                patch.object(self.job,'scan',return_value=True), \
                patch('scheduled_booking.sleep_until'),patch('scheduled_booking.datetime') as clock:
            clock.now.return_value=datetime(2026,10,19,7,1,tzinfo=CHINA)
            self.assertEqual(self.job.run(MagicMock(),'test-user','test-password'),0)
        prepare.assert_called_once_with(self.client,fresh=True)
        context.close.assert_called_once()

    def test_fresh_tabs_are_read_directly_and_second_venue_uses_its_own_tab(self):
        self.job.fast=True
        second=MagicMock()
        venues=self.config['venues']
        self.job.clients={venues[0]:self.client,venues[1]:second}
        self.job.prepared=set(venues);self.job.fresh=set(venues)
        self.client.find_choice.return_value=[]
        second.find_choice.return_value=[dict(slot,classes=['free']) for slot in self.choice]
        second.submit.return_value={'tradeNo':'second-tab-order'}
        with patch('scheduled_booking.available_choice',side_effect=[[],self.choice]):
            self.assertTrue(self.job.scan(self.client))
        for tab in (self.client,second):
            tab.open_venue.assert_not_called()
            tab.refresh_availability.assert_not_called()
        self.client.submit.assert_not_called()
        second.submit.assert_called_once()
        self.assertEqual(json.loads(self.job.state_path.read_text())['trade_no'],'second-tab-order')

    def test_refresh_started_at_release_is_finished_when_venue_is_checked(self):
        self.job.fast=True
        venue=self.config['venues'][0]
        waiter=MagicMock()
        self.job.clients={venue:self.client};self.job.prepared={venue}
        self.client.begin_refresh.return_value=waiter
        self.job.begin_refreshes()
        self.client.begin_refresh.assert_called_once()
        self.client.find_choice.return_value=[dict(slot,classes=['free']) for slot in self.choice]
        self.client.submit.return_value={'tradeNo':'refreshed-order'}
        self.assertTrue(self.job.scan(self.client))
        self.client.finish_refresh.assert_called_once_with(waiter)
        self.client.refresh_availability.assert_not_called()

    def test_warm_tabs_are_reused_and_refreshed_instead_of_reopened(self):
        self.job.fast=True
        second=MagicMock()
        venues=self.config['venues']
        session=MagicMock();session.take_tabs.return_value={venues[0]:self.client,venues[1]:second}
        self.job.session=session
        with patch('scheduled_booking.load_venue_tabs',return_value=set(venues)) as load, \
                patch('scheduled_booking.open_venue_tabs') as reopen:
            self.job.prepare_all(self.client,fresh=True)
        reopen.assert_not_called()
        self.assertFalse(load.call_args.kwargs['reopen'])
        self.assertEqual(self.job.fresh,set())
        self.client.begin_refresh.assert_called_once();second.begin_refresh.assert_called_once()
        self.job.begin_refreshes()
        self.client.begin_refresh.assert_called_once()
        self.job.release_tabs(self.client)
        session.keep_tabs.assert_called_once_with({venues[0]:self.client,venues[1]:second})
        second.page.close.assert_not_called()

    def test_progress_steps_are_recorded_with_separate_phases(self):
        self.job.fast=True
        self.client.find_choice.return_value=[dict(slot,classes=['free']) for slot in self.choice]
        phases=[]
        def submit(choice,**options):
            options['progress']('captcha_solve','正在识别并点选验证码。')
            phases.append(json.loads(self.job.state_path.read_text())['phase'])
            return {'tradeNo':'progress-order'}
        self.client.submit.side_effect=submit
        self.assertTrue(self.job.scan(self.client))
        self.assertEqual(phases,['captcha_solve'])
        self.assertIn('started_at',json.loads(self.job.state_path.read_text()))

    def test_shared_session_is_reused_and_never_closed_by_the_job(self):
        session=MagicMock()
        context=MagicMock()
        session.acquire.return_value=(context,self.client)
        self.job.session=session
        self.job.poll_availability=False
        with patch.object(self.job,'scan',return_value=True), \
                patch('scheduled_booking.authenticated_client') as fresh_login, \
                patch('scheduled_booking.sleep_until'),patch('scheduled_booking.datetime') as clock:
            clock.now.return_value=datetime(2026,10,19,7,1,tzinfo=CHINA)
            self.assertEqual(self.job.run(MagicMock(),'test-user','test-password'),0)
        session.acquire.assert_called_once()
        fresh_login.assert_not_called()
        context.close.assert_not_called()

    def test_immediate_job_returns_after_one_scan_without_polling(self):
        self.job.poll_availability=False
        self.job.read_successes=2
        context=MagicMock()
        with patch.object(self.job,'login',return_value=(context,self.client)), \
                patch.object(self.job,'scan',side_effect=[False,AssertionError('unexpected second scan')]) as scan, \
                patch('scheduled_booking.sleep_until') as wait,patch('scheduled_booking.datetime') as clock:
            clock.now.return_value=datetime(2026,10,19,7,1,tzinfo=CHINA)
            self.assertEqual(self.job.run(MagicMock(),'test-user','test-password'),3)
        scan.assert_called_once()
        wait.assert_called_once_with(self.job.start,self.job.check_cancelled)
        self.assertEqual(json.loads(self.job.state_path.read_text())['status'],'no_availability')
        self.client.submit.assert_not_called()

    def test_keeps_session_alive_and_reauthenticates_only_after_expiry(self):
        now=[datetime(2026,10,19,6,5,tzinfo=CHINA)]
        until=datetime(2026,10,19,6,16,tzinfo=CHINA)
        first_context,new_context,new_client=MagicMock(),MagicMock(),MagicMock()
        self.client.check_session.side_effect=[True,False]
        new_client.check_session.return_value=True
        self.job.login_state=MagicMock()
        self.job.prepared={self.config['venues'][0]}
        with patch('scheduled_booking.datetime') as clock, \
                patch('scheduled_booking.sleep_until',side_effect=lambda target,*args:now.__setitem__(0,target)), \
                patch.object(self.job,'login',return_value=(new_context,new_client)) as login:
            clock.now.side_effect=lambda zone:now[0]
            result=self.job.keep_session_until(MagicMock(),first_context,self.client,'user','password',until)
        self.assertEqual(result,(new_context,new_client))
        login.assert_called_once()
        first_context.close.assert_called_once()
        self.assertEqual(self.job.prepared,set())
        self.assertEqual(self.job.login_state.save.call_count,2)
        self.client.submit.assert_not_called()
        new_client.submit.assert_not_called()

    def test_daily_defaults_roll_dates_and_keep_evening_slots(self):
        config=load_config(DEFAULT_CONFIG)
        planned=daily_config(config,datetime(2026,12,31,20,tzinfo=CHINA))
        self.assertEqual(planned['start_at'],'2027-01-01T06:05:00+08:00')
        self.assertEqual(planned['release_at'],'2027-01-01T07:00:00+08:00')
        self.assertEqual(planned['reserve_date'],'2027-01-03')
        self.assertEqual(planned['reserve_time'],[20,21])
        self.assertEqual(len(planned['venues']),2)
        self.assertFalse(any('沙河' in venue for venue in planned['venues']))

    def test_daily_job_started_before_release_stays_on_current_day(self):
        planned=daily_config(load_config(DEFAULT_CONFIG),datetime(2026,10,20,6,30,tzinfo=CHINA))
        self.assertEqual(planned['start_at'],'2026-10-20T06:05:00+08:00')
        self.assertEqual(planned['reserve_date'],'2026-10-22')

    def test_daily_schedule_rejects_invalid_time_order_or_interval(self):
        for change in [{'login_at':'07:01:00'},{'release_at':'06:00:00'},{'keep_alive_seconds':0}]:
            config=load_config(DEFAULT_CONFIG)
            config['daily_schedule'].update(change)
            with self.subTest(change=change), self.assertRaises(ReservationError):
                daily_config(config,datetime(2026,10,20,tzinfo=CHINA))

    def test_stop_request_is_checked_before_any_submission(self):
        (self.root/'cancel.request').write_text('stop')
        with self.assertRaises(TaskStopped):self.job.scan(self.client)
        self.client.submit.assert_not_called()
        self.assertEqual(json.loads(self.job.state_path.read_text())['status'],'stopped')

    def test_uncertain_submission_stops_before_trying_other_venue(self):
        self.client.submit.side_effect = ReservationError('未收到订单响应')
        with patch('scheduled_booking.available_choice', return_value=self.choice):
            with self.assertRaises(ReservationError):
                self.job.scan(self.client)
        self.assertEqual(self.client.open_venue.call_count, 1)
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(json.loads(self.job.state_path.read_text())['status'], 'needs_attention')

    def test_unopened_date_never_submits(self):
        self.client.choose_date.side_effect = ReservationError('尚未开放')
        self.assertFalse(self.job.scan(self.client))
        self.client.submit.assert_not_called()

    def test_read_timeout_is_retried_before_submission(self):
        self.client.open_venue.side_effect=[BrowserTimeout('read timeout'),None]
        self.client.submit.return_value={'tradeNo':'example-trade'}
        with patch('scheduled_booking.available_choice',return_value=self.choice):
            self.assertTrue(self.job.scan(self.client))
        self.assertEqual(self.client.open_venue.call_count,2)
        self.assertEqual(self.job.read_failures,1)
        self.client.submit.assert_called_once()

    def test_query_log_saves_each_target_slot(self):
        slots=[dict(slot, classes=['reserved'],text='已售') for slot in self.choice]
        self.client.inspect.return_value=slots
        with patch('scheduled_booking.available_choice',return_value=[]):
            self.assertFalse(self.job.scan(self.client))
        lines=(self.root/'queries.jsonl').read_text().splitlines()
        self.assertEqual(len(lines),2)
        self.assertEqual(json.loads(lines[0])['target_slots'][0]['classes'],['reserved'])

    def test_rerun_preserves_locked_state_and_secret(self):
        import yaml
        config = self.root/'config.yaml'
        config.write_text(yaml.safe_dump(self.config,allow_unicode=True))
        secret = self.root/'secret.json'
        secret.write_text('{}')
        self.job.status('locked', trade_no='existing-order')
        with patch('scheduled_booking.target_date', return_value=date(2026,10,21)):
            result = main(['--config',str(config),'--state',str(self.job.state_path),'--secret',str(secret)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(self.job.state_path.read_text())['status'], 'locked')
        self.assertTrue(secret.exists())

    def test_qr_failure_preserves_order_and_stops_other_venues(self):
        self.job.payment_qr=True
        self.job.config['max_pay_amount']='100.00'
        choice=[dict(slot,text='￥25.00') for slot in self.choice]
        self.client.submit.return_value={'tradeNo':'test-trade'}
        with patch('scheduled_booking.available_choice',return_value=choice), patch('scheduled_booking.CampusQRCode') as factory:
            factory.return_value.create.side_effect=PaymentError('二维码读取失败')
            with self.assertRaises(PaymentError):self.job.scan(self.client)
        self.client.submit.assert_called_once()
        self.assertEqual(self.job.order,{'tradeNo':'test-trade'})
        self.assertEqual(json.loads(self.job.state_path.read_text())['status'],'qr_needs_attention')


if __name__ == '__main__':
    unittest.main()
