"""每日预约：6点后维持登录，7点预约学院路20:00–22:00，确认一个订单后停止。"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, time as wall_time
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlencode, urlparse

from playwright.sync_api import Error as BrowserError, sync_playwright
from cgyy import BASE_URL, CHINA, DEFAULT_CONFIG, LOG, ReservationError, SubmissionRejected, TaskStopped, VenueClient, authenticated_client, available_choice, load_config, sleep_until, target_date
from login_state import DEFAULT_SESSION_STATE, LoginState
from payment import CampusQRCode, ExpectedOrder, PaymentError, amount
from text_captcha import CaptchaError, TextCaptchaSolver


def daily_config(config: dict, now: datetime | None = None) -> dict:
    now = (now or datetime.now(CHINA)).astimezone(CHINA)
    schedule = config.get('daily_schedule', {})
    try:
        times = [wall_time.fromisoformat(schedule.get(key, fallback)) for key, fallback in
                 [('login_at','06:05:00'),('release_at','07:00:00'),('stop_at','08:00:00')]]
        interval = int(schedule.get('keep_alive_seconds', 300))
    except (ValueError, TypeError, AttributeError) as error:
        raise ReservationError('daily_schedule 时间应为 HH:MM:SS，登录检查间隔应为正整数。') from error
    if (any(value.tzinfo is not None for value in times) or
            not times[0] < times[1] < times[2] or interval < 30):
        raise ReservationError('登录、开始预约、截止时间须依次递增；检查间隔至少30秒。')
    day = now.date()
    if now >= datetime.combine(day,times[2],CHINA):
        day += timedelta(days=1)
    result = dict(config)
    for key,value in zip(['start_at','release_at','stop_at'],times):
        result[key] = datetime.combine(day,value,CHINA).isoformat()
    result['reserve_date'] = str(target_date(dict(config,reserve_date=None), day))
    result['keep_alive_seconds'] = interval
    return result


class BookingJob:
    def __init__(self, config: dict, state_path: Path, *, payment_qr=False, fast=False, captcha_solver=None, login_state=None, poll_availability=True):
        self.config = config
        self.state_path = state_path
        self.desired = target_date(config)
        self.start = datetime.fromisoformat(config['start_at']).astimezone(CHINA)
        self.release = datetime.fromisoformat(config['release_at']).astimezone(CHINA)
        self.stop = datetime.fromisoformat(config['stop_at']).astimezone(CHINA)
        self.submission_started = False
        self.order = None
        self.payment_qr = payment_qr
        self.poll_availability = poll_availability
        self.read_successes = 0
        self.read_failures = 0
        self.fast = fast
        self.captcha_solver = captcha_solver
        self.prepared_venue = None
        self.login_state = login_state if login_state is not None else LoginState()
        self.keep_alive_seconds = int(config.get('keep_alive_seconds', 300))
        if self.keep_alive_seconds < 30:
            raise ReservationError('登录检查间隔至少30秒。')
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
        deadline = started + (10 if self.fast else 20)
        period = f"{self.config['reserve_time'][0]:02}:00–{self.config['reserve_time'][-1]+1:02}:00"
        for venue in self.config['venues']:
            self.check_cancelled()
            if self.submission_started:
                raise ReservationError('已经尝试过提交，停止以免重复下单。')
            client.config = dict(self.config, venue=venue)
            self.status('checking', venue=venue, phase='open_venue', message=f'正在加载{venue}。')
            slots = None
            for attempt in range(2):
                phase = 'open_venue'
                try:
                    with client.reading_until(deadline):
                        if self.fast and self.prepared_venue == venue:
                            self.prepared_venue = None
                            client.refresh_availability()
                        else:
                            client.open_venue()
                        phase = 'choose_date'
                        self.status('checking', phase=phase, message=f'正在读取{self.desired}的{period}余票。')
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
                    LOG.info('读取失败详情：%s', str(error).splitlines()[0])
                    if time.monotonic() >= deadline:
                        break
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
                LOG.info('%s 无满足 %s 的连续场地。', venue, period)
                continue
            if self.fast and time.monotonic() - started >= 10:
                raise ReservationError('本轮查场已超过10秒预算，未提交订单。')
            expected = None
            if self.payment_qr:
                expected = ExpectedOrder.from_choice('',venue,self.desired,chosen,amount(self.config.get('max_pay_amount','70.00')))
            self.check_cancelled()
            self.submission_started = True
            self.status('submitting', venue=venue, court=chosen[0]['court'])
            try:
                if self.fast or self.captcha_solver:
                    timeout = max(1, int((10 - (time.monotonic() - started)) * 1000)) if self.fast else 45000
                    data = client.submit(chosen, timeout=timeout, captcha_solver=self.captcha_solver)
                else:
                    data = client.submit(chosen)
            except SubmissionRejected as error:
                self.status('rejected',message=str(error))
                raise
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
                        date=str(self.desired), slots=[(cell['court'],cell['start'],cell['end']) for cell in chosen],
                        payment_url=payment_url, booking_elapsed_seconds=round(time.monotonic() - started, 3),
                        message='订单已生成，请本人尽快登录网站，在页面倒计时内付款。')
            if expected is not None:
                from dataclasses import replace
                payer = CampusQRCode(client.page,replace(expected,trade_no=data['tradeNo']))
                try:
                    qr = payer.create()
                    self.status('awaiting_payment', **qr, message=qr.get('scan_tip','请在倒计时内扫描官方二维码付款。'))
                    payer.wait_for_payment(qr['expires_at'], self.status)
                except (PaymentError,BrowserError) as error:
                    self.status('qr_needs_attention',message=str(error) if isinstance(error,PaymentError) else '订单已生成，二维码或支付状态读取失败，请到场馆网站核对，不重复下单。',qr_code=None)
                    raise
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

    def check_cancelled(self):
        if (self.state_path.parent/'cancel.request').exists():
            self.status('stopped',message='预约任务已停止。')
            raise TaskStopped('预约任务已停止。')

    def keep_session_until(self, browser, context, client, user: str, password: str, until: datetime):
        while datetime.now(CHINA) < until:
            check_at = min(datetime.now(CHINA) + timedelta(seconds=self.keep_alive_seconds), until)
            self.status('keeping_session', next_check_at=check_at.isoformat(), release_at=self.release.isoformat())
            sleep_until(check_at,self.check_cancelled)
            if client.check_session(user):
                self.login_state.save(context, user)
                self.status('session_valid', checked_at=datetime.now(CHINA).isoformat())
            else:
                context.close()
                context, client = self.login(browser, user, password)
                self.prepared_venue = None
        return context, client

    def run(self, browser, user: str, password: str) -> int:
        if self.config.get('account_username',user)!=user:
            raise ReservationError('预约配置与当前登录账号不一致，请重新保存预约规则。')
        self.status('waiting_start',username=user)
        sleep_until(self.start,self.check_cancelled)
        if datetime.now(CHINA) >= self.stop:
            self.status('expired', message='已过本次任务截止时间，未尝试下单。')
            return 3
        context, client = self.login(browser, user, password)
        try:
            if datetime.now(CHINA) < self.release:
                self.status('waiting_release', release_at=self.release.isoformat())
                context, client = self.keep_session_until(browser, context, client, user, password,
                                                         self.release - timedelta(minutes=5))
                if self.fast:
                    client.config = dict(self.config, venue=self.config['venues'][0])
                    client.prepare_booking(self.desired)
                    self.prepared_venue = self.config['venues'][0]
                    self.status('ready', venue=self.prepared_venue, release_at=self.release.isoformat())
                else:
                    client.list_venues()
                # 最后15秒前再检查；检查过程不离开已准备的场馆页面。
                context, client = self.keep_session_until(browser, context, client, user, password,
                                                         self.release - timedelta(seconds=15))
                if self.fast and self.prepared_venue is None:
                    client.config = dict(self.config, venue=self.config['venues'][0])
                    client.prepare_booking(self.desired)
                    self.prepared_venue = self.config['venues'][0]
                sleep_until(self.release,self.check_cancelled)
            while datetime.now(CHINA) < self.stop:
                if self.scan(client):
                    return 0
                if not self.poll_availability:
                    break
                self.status('waiting_availability', message='本轮未找到空场，30秒后再次查询。')
                sleep_until(min(datetime.now(CHINA) + timedelta(seconds=30), self.stop),self.check_cancelled)
            if self.read_successes:
                self.status('no_availability', successful_queries=self.read_successes, read_errors=self.read_failures,
                            message='本次查询未找到所选时段的连续可预约场地，未下单。' + ('部分场馆读取失败，可稍后重新查询。' if self.read_failures else ''))
                return 3
            self.status('failed', successful_queries=0, read_errors=self.read_failures,
                        message='没有完成有效查询，不能据此判断场地是否已满。')
            return 1
        finally:
            context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--daily', action='store_true', help='按每日时间表运行，预约日期随运行日期滚动')
    parser.add_argument('--secret', type=Path)
    parser.add_argument('--state', type=Path, help='任务状态；每日模式默认按日期独立保存')
    parser.add_argument('--validate', action='store_true', help='只校验配置和运行环境，不访问网站')
    parser.add_argument('--fast', action='store_true', help='开放前准备场馆，开放后10秒下单预算，找到可用场地即停止翻页')
    parser.add_argument('--headed', action='store_true', help='使用有界面的标准 Chromium；服务器需 DISPLAY/Xvfb')
    parser.add_argument('--browser-binary', default=os.getenv('CGYY_BROWSER_BINARY'), help='自定义 Chromium 路径')
    parser.add_argument('--session-state', type=Path, default=DEFAULT_SESSION_STATE, help='本机北航登录态缓存')
    parser.add_argument('--no-session-cache', action='store_true', help='禁用登录态缓存')
    parser.add_argument('--captcha-repo', type=Path, help='Text_select_captcha 本地仓库')
    parser.add_argument('--captcha-font', type=Path, default=Path('/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'))
    parser.add_argument('--payment-qr', action='store_true', help='下单后返回官方校园付二维码')
    parser.add_argument('--max-pay-amount')
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    job = None
    lock_file = None
    try:
        config = load_config(args.config)
        daily = args.daily or not any(config.get(key) for key in ['start_at','release_at','stop_at'])
        if daily:
            config = daily_config(config)
            args.fast = args.fast or config.get('fast',True)
        if args.browser_binary:
            config['browser_binary'] = args.browser_binary
        if args.state is None:
            if not daily:
                raise ReservationError('一次性任务请指定 --state。')
            args.state = Path('artifacts/daily') / datetime.fromisoformat(config['start_at']).date().isoformat() / 'status.json'
        config['max_pay_amount'] = args.max_pay_amount or config.get('max_pay_amount') or os.getenv('CGYY_MAX_PAY_AMOUNT','70.00')
        job = BookingJob(config,args.state,payment_qr=args.payment_qr,fast=args.fast,
                         login_state=LoginState(None if args.no_session_cache or not config.get('remember_session',True) else args.session_state))
        if args.validate:
            if config.get('browser_binary') and not Path(config['browser_binary']).is_file():
                raise ReservationError('指定浏览器不存在。')
            print(json.dumps({'start_at':job.start.isoformat(), 'reserve_date':str(job.desired),
                              'release_at':job.release.isoformat(), 'hours':config['reserve_time'], 'venues':config['venues'],
                              'stop_at':job.stop.isoformat(), 'keep_alive_seconds':job.keep_alive_seconds,
                              'state_path':str(args.state)}, ensure_ascii=False))
            return 0
        args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_file = Path('artifacts/booking-execution.lock').open('w')
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error('已有同一预约任务正在执行，拒绝重复启动。')
            return 1
        if args.state.exists() and json.loads(args.state.read_text()).get('status') in {
                'locked','submitting','needs_attention','paid','awaiting_payment','qr_needs_attention'}:
            LOG.error('此前已提交或提交状态不明；先核对已有订单，拒绝重复执行。')
            return 1
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
            options = {'headless':not args.headed}
            if config.get('browser_binary'):
                options['executable_path'] = config['browser_binary']
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
            job.status('stopped' if isinstance(error,TaskStopped) else 'failed', message='任务已停止。' if isinstance(error,TaskStopped) else '预约程序未完成，请查看运行日志或登录网站检查。')
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
