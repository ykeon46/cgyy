"""手机推送：各服务的请求格式、状态到消息的映射、去重，以及真实投递到本机服务。"""
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import notify
from cgyy import DEFAULT_CONFIG, load_config
from scheduled_booking import BookingJob

ORDER = {'venue': '学院路校区-体育馆主馆-羽毛球', 'date': '2026-11-03', 'slots': [['1号', '20:00', '21:00']],
         'amount': '15.00', 'expires_at': '2026-11-01T20:01:30+08:00'}


class RequestFormatTests(unittest.TestCase):
    def test_bark_opens_alipay_link_with_time_sensitive_level(self):
        request = notify.build_request('https://api.day.app/KEY', '标题', '正文', 'https://qr.alipay.com/x', True)
        self.assertEqual(request.full_url, 'https://api.day.app/KEY')
        body = json.loads(request.data)
        self.assertEqual((body['title'], body['url'], body['level']), ('标题', 'https://qr.alipay.com/x', 'timeSensitive'))

    def test_serverchan_uses_form_fields_with_markdown_link(self):
        request = notify.build_request('https://sctapi.ftqq.com/KEY.send', '标题', '正文', 'https://qr.alipay.com/x')
        fields = parse_qs(request.data.decode())
        self.assertEqual(fields['title'], ['标题'])
        self.assertIn('(https://qr.alipay.com/x)', fields['desp'][0])

    def test_ntfy_publishes_json_to_server_root_with_topic(self):
        request = notify.build_request('https://ntfy.sh/my-topic', '中文标题', '正文', 'https://qr.alipay.com/x', True)
        self.assertEqual(request.full_url, 'https://ntfy.sh/')
        body = json.loads(request.data)
        self.assertEqual((body['topic'], body['title'], body['click'], body['priority']),
                         ('my-topic', '中文标题', 'https://qr.alipay.com/x', 5))

    def test_self_hosted_services_use_explicit_prefix(self):
        request = notify.build_request('bark+https://push.example.com/KEY', '标题', '正文')
        self.assertEqual(request.full_url, 'https://push.example.com/KEY')
        self.assertEqual(json.loads(request.data)['group'], 'cgyy')

    def test_invalid_or_missing_address_sends_nothing(self):
        with patch.dict(os.environ, {'CGYY_NOTIFY_URL': ''}):
            self.assertIsNone(notify.send('标题', '正文'))
        self.assertIsNone(notify.send('标题', '正文', endpoint='file:///etc/passwd'))


class MessageTests(unittest.TestCase):
    def test_final_alipay_code_links_directly_to_alipay(self):
        key, title, body, url, urgent = notify.booking_message(dict(ORDER, status='awaiting_payment', phase='payment_ready',
            qr_kind='alipay_payment', alipay_url='https://qr.alipay.com/abc'))
        self.assertEqual((key, url, urgent), ('pay', 'https://qr.alipay.com/abc', True))
        self.assertIn('20:01:30', body)
        self.assertIn('1号 20:00-21:00', body)

    def test_campus_entry_before_alipay_code_is_not_pushed(self):
        self.assertIsNone(notify.booking_message(dict(ORDER, status='awaiting_payment', phase='alipay_qr')))

    def test_campus_fallback_links_to_cashier(self):
        message = notify.booking_message(dict(ORDER, status='awaiting_payment', phase='payment_ready',
                                              qr_kind='campus_cashier', cashier_url='https://cashier.cc-pay.cn/c'))
        self.assertEqual(message[3], 'https://cashier.cc-pay.cn/c')

    def test_progress_states_are_silent(self):
        for status in ['queued', 'logging_in', 'checking', 'submitting', 'ready']:
            self.assertIsNone(notify.booking_message({'status': status}))


class JobNotificationTests(unittest.TestCase):
    def test_each_event_is_pushed_once(self):
        with tempfile.TemporaryDirectory() as folder, patch('scheduled_booking.target_date', return_value=date(2026, 11, 3)):
            config = dict(load_config(DEFAULT_CONFIG), start_at='2026-11-01T06:00:00+08:00',
                          release_at='2026-11-01T07:00:00+08:00', stop_at='2026-11-01T08:00:00+08:00')
            job = BookingJob(config, Path(folder) / 'status.json')
            with patch('scheduled_booking.notify.send') as send:
                job.status('locked', **ORDER, payment_url='https://cgyy.buaa.edu.cn/venue/pay-order?tradeNo=T')
                job.status('locked', message='重复写入')
                job.status('awaiting_payment', phase='alipay_qr', qr_kind='campus_cashier')
                job.status('awaiting_payment', phase='payment_ready', qr_kind='alipay_payment',
                           alipay_url='https://qr.alipay.com/abc')
                job.status('paid')
        self.assertEqual([call.args[0] for call in send.call_args_list],
                         ['已抢到场地，正在生成付款码', '点开立即用支付宝付款', '预约成功'])
        self.assertEqual(send.call_args_list[1].args[2], 'https://qr.alipay.com/abc')


class DeliveryTests(unittest.TestCase):
    def test_push_reaches_http_endpoint(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200);self.end_headers()
            def log_message(self, *args):pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}):
            thread = notify.send('预约成功', '正文', endpoint=f'http://127.0.0.1:{server.server_port}/hook')
            thread.join(5)
        self.assertEqual(received, [{'title': '预约成功', 'body': '正文', 'url': None, 'urgent': False}])


if __name__ == '__main__':
    unittest.main()
