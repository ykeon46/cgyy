"""一次性后台预约：到点检查指定场馆，确认一个订单后停止，由账号本人付款。"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlencode, urlparse

from playwright.sync_api import Error as BrowserError, sync_playwright
from cgyy import BASE_URL, CHINA, LOG, ReservationError, VenueClient, authenticated_client, available_choice, load_config, sleep_until, target_date
from login_state import DEFAULT_SESSION_STATE, LoginState
from payment import AlipayPayment, ExpectedOrder, PaymentError, PaymentSettings, check_previous_result
from text_captcha import CaptchaError, TextCaptchaSolver


class BookingJob:
    def __init__(self, config: dict, state_path: Path, *, payment_settings=None, auto_pay=False, payment_preview=False, fast=False, captcha_solver=None, login_state=None):
        self.config = config
        self.state_path = state_path
        self.desired = target_date(config)
        self.start = datetime.fromisoformat(config['start_at']).astimezone(CHINA)
        self.release = datetime.fromisoformat(config['release_at']).astimezone(CHINA)
        self.stop = datetime.fromisoformat(config['stop_at']).astimezone(CHINA)
        self.submission_started = False
        self.order = None
        self.payment_settings = payment_settings
        self.auto_pay = auto_pay
        self.payment_preview = payment_preview
        self.read_successes = 0
        self.read_failures = 0
        self.fast = fast
        self.captcha_solver = captcha_solver
        self.prepared_venue = None
        self.login_state = login_state if login_state is not None else LoginState()
        venues = config.get('venues', [])
        if not venues or any(not v.startswith('学院路校区-') or not v.endswith('-羽毛球') for v in venues):
            raise ReservationError('此任务仅允许学院路校区羽毛球场，排除沙河。')
        if not self.start < self.release < self.stop or self.stop.date() != self.start.date():
            raise ReservationError('开始、开放、截止时间必须依次递增且在同一天。')

    def status(self, status: str, **details) -> None:
        previous = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        value = dict(previous, status=status, updated_at=datetime.now(CHINA).isoformat(), **details)
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(self.state_path)
        LOG.info('任务状态：%s', status)

    def scan(self, client: VenueClient) -> bool:
        started = time.monotonic()
        for venue in self.config['venues']:
            if self.submission_started:
                raise ReservationError('已经尝试过提交，停止以免重复下单。')
            client.config = dict(self.config, venue=venue)
            self.status('checking', venue=venue)
            slots = None
            for attempt in range(2):
                phase = 'open_venue'
                try:
                    if self.fast and self.prepared_venue == venue:
                        self.prepared_venue = None
                        client.refresh_availability()
                    else:
                        client.open_venue()
                    phase = 'choose_date'
                    client.choose_date(self.desired)
                    phase = 'inspect'
                    slots = client.find_choice() if self.fast else client.inspect()
                    break
                except ReservationError as error:
                    LOG.info('%s：%s', venue, error)
                    break
                except BrowserError as error:
                    self.read_failures += 1
                    # 此处尚未提交订单，可以安全重试读取；保留失败环节。
                    current_url = client.page.url
                    path = urlparse(current_url).path if isinstance(current_url, str) else ''
                    self.status('read_retry' if attempt == 0 else 'read_failed',
                                venue=venue, phase=phase, error_type=type(error).__name__, page_path=path)
                    LOG.warning('%s：读取 %s 失败（%s），%s。', venue, phase,
                                type(error).__name__, '重试一次' if attempt == 0 else '继续检查其他场馆')
            if slots is None:
                continue
            self.read_successes += 1
            hours = {f'{hour:02}:00' for hour in self.config['reserve_time']}
            details = [{key: slot.get(key) for key in ['court','start','end','classes','text']}
                       for slot in slots if slot['start'] in hours]
            audit = {'checked_at':datetime.now(CHINA).isoformat(), 'venue':venue,
                     'date':str(self.desired), 'target_slots':details}
            with (self.state_path.parent / 'queries.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(audit,ensure_ascii=False) + '\n')
            chosen = available_choice(slots, client.config)
            if not chosen:
                LOG.info('%s 无满足 20:00–22:00 的连续场地。', venue)
                continue
            if self.fast and time.monotonic() - started >= 10:
                raise ReservationError('本轮查场已超过10秒预算，未提交订单。')
            expected = None
            if self.auto_pay or self.payment_preview:
                expected = ExpectedOrder.from_choice('', venue, self.desired, chosen, self.payment_settings.limit)
            self.submission_started = True
            self.status('submitting', venue=venue, court=chosen[0]['court'])
            try:
                if self.fast or self.captcha_solver:
                    timeout = max(1, int((10 - (time.monotonic() - started)) * 1000)) if self.fast else 45000
                    data = client.submit(chosen, timeout=timeout, captcha_solver=self.captcha_solver)
                else:
                    data = client.submit(chosen)
            except (ReservationError, BrowserError, CaptchaError):
                # 提交超时也可能已经生成订单，绝不能换场馆再下单。
                self.status('needs_attention', message='提交未确认成功；请先登录个人中心核对订单，不自动重试。')
                try:
                    client.page.screenshot(path=str(self.state_path.parent / 'needs-attention.png'), full_page=True)
                except BrowserError:
                    pass
                raise
            self.order = data
            payment_url = BASE_URL + '/venue/pay-order?' + urlencode({'tradeNo': data['tradeNo']})
            self.status('locked', venue=venue, court=chosen[0]['court'], trade_no=data['tradeNo'],
                        payment_url=payment_url, booking_elapsed_seconds=round(time.monotonic() - started, 3),
                        message='订单已生成，请本人尽快登录网站，在页面倒计时内付款。')
            if expected is not None:
                from dataclasses import replace
                expected = replace(expected, trade_no=data['tradeNo'])
                payer = AlipayPayment(client.page, expected, self.payment_settings,
                                      self.state_path.parent/'payment-result.json', charge=self.auto_pay)
                try:
                    result = payer.run()
                except PaymentError:
                    self.status('payment_unknown' if payer.submitted else 'payment_needs_attention',
                                message='付款流程停止，请先核对支付宝及场馆订单，不重新下单。')
                    raise
                self.status(result, message='已核对场馆订单支付成功。' if result == 'paid' else '已打开支付宝付款页，未提交付款。')
                return True
            try:
                client.page.wait_for_timeout(1000)
                client.page.screenshot(path=str(self.state_path.parent / 'order.png'), full_page=True)
            except BrowserError:
                pass
            return True
        return False

    def login(self, browser, user: str, password: str):
        self.status('logging_in')
        return authenticated_client(browser, self.config, user, lambda:password, self.login_state,
                                    ignore_password_warning=True)

    def run(self, browser, user: str, password: str) -> int:
        self.status('waiting_start')
        sleep_until(self.start)
        if datetime.now(CHINA) >= self.stop:
            self.status('expired', message='已过本次任务截止时间，未尝试下单。')
            return 3
        context, client = self.login(browser, user, password)
        try:
            if not self.fast and self.scan(client):
                return 0
            if datetime.now(CHINA) < self.release:
                self.status('waiting_release', release_at=self.release.isoformat())
                # 开放前重新验证登录态；仅失效时重新登录。
                sleep_until(self.release - timedelta(minutes=5))
                context.close()
                context, client = self.login(browser, user, password)
                if self.fast:
                    client.config = dict(self.config, venue=self.config['venues'][0])
                    client.prepare_booking(self.desired)
                    self.prepared_venue = self.config['venues'][0]
                    self.status('ready', venue=self.prepared_venue, release_at=self.release.isoformat())
                else:
                    client.list_venues()
                sleep_until(self.release)
            while datetime.now(CHINA) < self.stop:
                if self.scan(client):
                    return 0
                self.status('waiting_availability')
                sleep_until(min(datetime.now(CHINA) + timedelta(seconds=30), self.stop))
            if self.read_successes:
                self.status('no_availability', successful_queries=self.read_successes, read_errors=self.read_failures,
                            message='已完成的查询未找到连续两小时的可预约场地，逐场地明细见 queries.jsonl。')
                return 3
            self.status('failed', successful_queries=0, read_errors=self.read_failures,
                        message='没有完成有效查询，不能据此判断场地是否已满。')
            return 1
        finally:
            context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--secret', type=Path)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--validate', action='store_true', help='只校验配置和运行环境，不访问网站')
    parser.add_argument('--fast', action='store_true', help='开放前准备场馆，开放后10秒下单预算，找到可用场地即停止翻页')
    parser.add_argument('--headed', action='store_true', help='使用有界面的标准 Chromium；服务器需 DISPLAY/Xvfb')
    parser.add_argument('--session-state', type=Path, default=DEFAULT_SESSION_STATE, help='本机北航登录态缓存')
    parser.add_argument('--no-session-cache', action='store_true', help='禁用登录态缓存')
    parser.add_argument('--captcha-repo', type=Path, help='Text_select_captcha 本地仓库')
    parser.add_argument('--captcha-font', type=Path, default=Path('/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'))
    pay_mode = parser.add_mutually_exclusive_group()
    pay_mode.add_argument('--auto-pay', action='store_true', help='下单后提交支付宝付款，可能实际扣款')
    pay_mode.add_argument('--payment-preview', action='store_true', help='打开支付宝付款页，不提交付款')
    parser.add_argument('--max-pay-amount', help='人民币单笔上限，默认读取 CGYY_MAX_PAY_AMOUNT')
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    job = None
    lock_file = None
    try:
        config = load_config(args.config)
        enabled = args.auto_pay or args.payment_preview
        settings = PaymentSettings.from_env(args.max_pay_amount, charge=args.auto_pay) if enabled else None
        job = BookingJob(config, args.state, payment_settings=settings, auto_pay=args.auto_pay, payment_preview=args.payment_preview, fast=args.fast,
                         login_state=LoginState(None if args.no_session_cache else args.session_state))
        if args.validate:
            if not Path(config['browser_binary']).is_file():
                raise ReservationError('指定浏览器不存在。')
            print(json.dumps({'start_at':job.start.isoformat(), 'reserve_date':str(job.desired),
                              'hours':config['reserve_time'], 'venues':config['venues'], 'stop_at':job.stop.isoformat()}, ensure_ascii=False))
            return 0
        args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_file = (args.state.parent / 'execution.lock').open('w')
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error('已有同一预约任务正在执行，拒绝重复启动。')
            return 1
        if args.state.exists() and json.loads(args.state.read_text()).get('status') in {
                'locked','submitting','needs_attention','paid','payment_unknown','payment_needs_attention','payment_preview_ready'}:
            LOG.error('此前已提交或提交状态不明；先核对已有订单，拒绝重复执行。')
            return 1
        if enabled:
            check_previous_result(args.state.parent/'payment-result.json')
        if args.captcha_repo:
            job.captcha_solver = TextCaptchaSolver(args.captcha_repo, args.captcha_font)
        secret = {}
        if args.secret:
            secret = json.loads(args.secret.read_text(encoding='utf-8'))
            args.secret.unlink()
        user = os.getenv('BUAA_SSO_USER') or secret.get('user')
        password = os.getenv('BUAA_SSO_PASSWORD') or secret.get('password')
        secret.clear()
        if not user:
            raise ReservationError('需要 BUAA_SSO_USER，或一次性登录凭据文件中的用户名。')
        with sync_playwright() as playwright:
            options = {'headless':not args.headed, 'executable_path':config['browser_binary']}
            if config.get('proxy'):
                options['proxy'] = {'server':config['proxy']}
            browser = playwright.chromium.launch(**options)
            try:
                return job.run(browser, user, password)
            finally:
                browser.close()
                password = ''
    except (ReservationError, PaymentError, BrowserError, OSError, ValueError, KeyError) as error:
        if not args.validate and job is not None and not job.order and not job.submission_started:
            job.status('failed', message='预约程序未完成，请查看运行日志或登录网站检查。')
        if isinstance(error, (ReservationError, PaymentError)):
            LOG.error('%s', error)
        else:
            LOG.error('运行失败（%s），未输出可能包含登录凭据的底层调用信息。', type(error).__name__)
        return 1
    finally:
        if lock_file is not None:
            lock_file.close()


if __name__ == '__main__':
    sys.exit(main())
