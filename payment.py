"""校园付到支付宝账户付款。凭据仅从环境变量读取，付款表单只提交一次。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout


class PaymentError(ValueError):
    """不包含付款凭据、票据或浏览器调用详情的错误。"""


def amount(value) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number != number.quantize(Decimal('0.01')):
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError):
        raise PaymentError('付款金额必须是大于零、最多两位小数的人民币金额。') from None


@dataclass(frozen=True)
class PaymentSettings:
    limit: Decimal
    account: str = field(default='', repr=False)
    password: str = field(default='', repr=False)

    @classmethod
    def from_env(cls, limit=None, *, charge: bool):
        ceiling = limit if limit is not None else os.getenv('CGYY_MAX_PAY_AMOUNT')
        if not ceiling:
            raise PaymentError('请设置 CGYY_MAX_PAY_AMOUNT 或 --max-pay-amount，作为单笔付款上限。')
        account = os.getenv('ALIPAY_ACCOUNT', '').strip()
        password = os.getenv('ALIPAY_PAY_PASSWORD', '')
        if charge and (not account or not password):
            raise PaymentError('自动付款需要 ALIPAY_ACCOUNT 和 ALIPAY_PAY_PASSWORD（支付宝支付密码）。')
        return cls(amount(ceiling), account, password)


def normalize(value: str) -> str:
    return re.sub(r'[\s\-()（）]', '', value)


@dataclass(frozen=True)
class ExpectedOrder:
    trade_no: str
    venue: str
    day: date
    slots: tuple[tuple[str, str, str], ...]
    fee: Decimal

    @classmethod
    def from_choice(cls, trade_no, venue, day, choice, limit):
        if not choice or '沙河' in venue:
            raise PaymentError('自动付款需要有效场次，且不允许沙河校区。')
        prices = []
        for slot in choice:
            match = re.fullmatch(r'\s*[￥¥]\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?\s*', slot.get('text', ''))
            if not match:
                raise PaymentError('未能确定所选场次的价格，未发起自动付款。')
            prices.append(amount(match[1]))
        total = sum(prices, Decimal('0'))
        if total > limit:
            raise PaymentError('订单价格超过单笔付款上限。')
        return cls(trade_no, venue, day,
                   tuple((s['court'], s['start'], s['end']) for s in choice), total)

    def verify(self, data: dict, *, payable=True):
        if amount(data.get('payFee')) != self.fee:
            raise PaymentError('服务器订单金额与所选场次不一致。')
        if normalize(data.get('subject') or '') != normalize(self.venue):
            raise PaymentError('服务器订单场馆与所选场馆不一致。')
        observed = []
        for slot in data.get('spaceList') or []:
            start = str(slot.get('startDate', '')).split(' ')
            end = str(slot.get('endDate', '')).split(' ')
            if not start or not end or start[0] != str(self.day) or end[0] != str(self.day):
                raise PaymentError('服务器订单日期与预约日期不一致。')
            observed.append((slot.get('venueSpaceName'), slot.get('startTime'), slot.get('endTime')))
        if sorted(observed) != sorted(self.slots):
            raise PaymentError('服务器订单场地或时段与选择不一致。')
        if str(data.get('orderStatus')) != '1':
            raise PaymentError('场馆订单已取消或失效，停止付款。')
        if payable:
            if str(data.get('payStatus')) != '1':
                raise PaymentError('场馆订单不是待付款状态，停止付款。')
            try:
                remaining = int(data['remainSecond'])
            except (TypeError, ValueError, KeyError):
                remaining = 0
            if remaining <= 5:
                raise PaymentError('场馆订单已过期或剩余时间不足，停止付款。')


READ_ORDER = r'''tradeNo => new Promise(resolve => {
    const node = Array.from(document.querySelectorAll('*')).find(e => e.__vue__ && e.__vue__.commonMethods);
    if (!node) return resolve({error:true});
    const timer = setTimeout(() => resolve({error:true}), 6000);
    node.__vue__.commonMethods.emitAjax({
        path:'/api/venue/finances/order/detail', data:{venueTradeNo:tradeNo},
        success:data=>{clearTimeout(timer);resolve({data});},
        error:()=>{clearTimeout(timer);resolve({error:true});}
    });
})'''

ALIPAY_ACCOUNT_READY = r'''() => {
    const module = window.seajs?.require?.('excashier/login/2022.7.27/loginPwdMemberT');
    return !!module && module._accountValidated === true && module.submitBtnDisabled === false
        && !module.ajaxValidateAccountSending && window.light?.page?.products?.payPasswd?.ready === true;
}'''


def require_origin(page, host):
    parsed = urlparse(page.url)
    if parsed.scheme != 'https' or parsed.netloc != host:
        raise PaymentError('付款页面域名异常，已停止。')


def check_previous_result(path: Path, trade_no: str | None = None):
    if not path.exists():
        return
    try:
        previous = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(previous, dict):
            raise ValueError
    except ValueError:
        raise PaymentError('付款结果记录无法读取，请先核对已有订单。') from None
    if previous.get('status') == 'payment_submitted_unknown':
        raise PaymentError('此前付款结果不明，请先核对账单及订单；未重新下单或付款。')
    if (trade_no and previous.get('trade_no') == trade_no
            and (previous.get('payer_form_submitted') or previous.get('status') == 'paid')):
        raise PaymentError('此订单已有付款提交记录，拒绝重复付款。')


class AlipayPayment:
    def __init__(self, venue_page, expected: ExpectedOrder, settings: PaymentSettings,
                 result_path: Path, *, charge: bool = False):
        self.venue_page = venue_page
        self.context = venue_page.context
        self.expected = expected
        self.settings = settings
        self.result_path = result_path
        self.charge = charge
        self.started = False
        self.submitted = False
        self.final_charge_submitted = False
        self.clock_started = time.monotonic()
        self.deadline = self.clock_started + 58
        self.state = {'trade_no':expected.trade_no, 'amount':str(expected.fee),
                      'payer_form_submitted':False, 'payment_request_observed':False, 'status':'not_started'}

    def record(self, status, **values):
        now = datetime.now().astimezone().isoformat()
        elapsed = round(time.monotonic() - self.clock_started, 3)
        events = self.state.setdefault('events', [])
        events.append({'status':status, 'at':now, 'elapsed_seconds':elapsed})
        self.state.update(status=status, updated_at=now, elapsed_seconds=elapsed, **values)
        self.result_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.result_path.with_name(self.result_path.name + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(self.state, stream, ensure_ascii=False, indent=2)
        temporary.chmod(0o600)
        temporary.replace(self.result_path)

    def read_order(self):
        require_origin(self.venue_page, 'cgyy.buaa.edu.cn')
        result = self.venue_page.evaluate(READ_ORDER, self.expected.trade_no)
        if not isinstance(result, dict) or not isinstance(result.get('data'), dict):
            raise PaymentError('读取场馆订单状态失败，停止付款。')
        remaining = result['data'].get('remainSecond')
        if isinstance(remaining, (int, float)) and remaining > 0:
            self.deadline = min(self.deadline, time.monotonic() + remaining - 1)
        return result['data']

    def timeout(self, maximum=12000):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PaymentError('已到本次付款时限，停止操作，不重新提交。')
        return max(1, min(maximum, int(remaining * 1000)))

    def cashier_page(self):
        deadline = min(time.monotonic() + 8, self.deadline)
        while time.monotonic() < deadline:
            for page in self.context.pages:
                parsed = urlparse(page.url)
                if (parsed.scheme == 'https' and parsed.netloc == 'cgyy.buaa.edu.cn'
                        and parse_qs(parsed.query).get('tradeNo') == [self.expected.trade_no]
                        and page.locator('.orderInfo-box:visible').count()):
                    return page
            self.venue_page.wait_for_timeout(150)
        raise PaymentError('未找到本订单的付款弹出页面，请核对已有订单，不重新下单。')

    def open_alipay(self):
        cashier = self.cashier_page()
        self.expected.verify(self.read_order())
        self.record('venue_order_verified')
        box = cashier.locator('.orderInfo-box:visible')
        model = box.evaluate('el=>el.__vue__ && el.__vue__.choosePayModel && el.__vue__.choosePayModel.payType')
        if str(model) != '13' or box.get_by_text('航财通·校园付', exact=True).count() != 1:
            raise PaymentError('当前支付渠道不是已核对的航财通·校园付，停止付款。')
        with cashier.expect_response(lambda r:urlparse(r.url).path.endswith('/api/venue/finances/order/pay'), timeout=self.timeout()) as pending:
            box.locator('.btn-group .btn').filter(has_text=re.compile(r'^\s*支付')).click()
        response = pending.value
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
            raise PaymentError('校园付返回无效交易信息，停止付款。')
        data = payload['data']
        parsed = urlparse(data.get('schoolPayUrl') or '')
        if (not response.ok or str(payload.get('code')) != '200'
                or amount(data.get('payFee')) != self.expected.fee
                or parsed.scheme != 'https' or parsed.netloc != 'cashier.cc-pay.cn'):
            raise PaymentError('校园付交易信息异常，停止付款。')
        self.record('campus_cashier_requested')
        with self.context.expect_page(timeout=self.timeout()) as popup:
            cashier.get_by_text('使用电脑支付', exact=True).click()
        alipay = popup.value
        alipay.wait_for_url('https://cashier.cc-pay.cn/**', wait_until='domcontentloaded', timeout=self.timeout(15000))
        require_origin(alipay, 'cashier.cc-pay.cn')
        self.record('campus_cashier_opened')
        alipay.locator('svg').filter(has=alipay.locator('use[*|href="#iconzhifubaozhifu1"]')).click()
        alipay.wait_for_url(re.compile(r'https://(?:excashier|cashier)\.alipay\.com/'), wait_until='domcontentloaded', timeout=self.timeout(15000))
        if urlparse(alipay.url).hostname == 'cashier.alipay.com':
            self.record('alipay_final_cashier_ready')
            return alipay
        require_origin(alipay, 'excashier.alipay.com')
        self.record('alipay_opened')
        if not alipay.locator('#J_tLoginId:visible').count():
            alipay.locator('#J_changePayStyle:visible').click(timeout=self.timeout(10000))
            try:
                alipay.locator('#J_tLoginId:visible').wait_for(timeout=1500)
            except BrowserTimeout:
                # 此页面的链接依赖旧版 JS；使用同页正式表单切换付款方式。
                switch = alipay.locator('#J_changePayStyleForm')
                destination = urlparse(switch.get_attribute('action') or '')
                if (destination.scheme != 'https' or destination.netloc not in {'excashier.alipay.com', 'excashier.alipay.com:443'}
                        or destination.path != '/standard/switchToStdFront.htm'
                        or (switch.get_attribute('method') or '').lower() != 'post'):
                    raise PaymentError('支付宝付款方式切换表单异常。')
                switch.evaluate('form=>form.requestSubmit()')
        alipay.locator('#J_tLoginId:visible').wait_for(timeout=self.timeout(10000))
        # 账户表单出现时，安全控件脚本仍可能在初始化；等待本次导航加载完成。
        alipay.wait_for_load_state('load', timeout=self.timeout())
        self.record('alipay_form_ready')
        return alipay

    @staticmethod
    def is_payment_request(request):
        parsed = urlparse(request.url)
        return (request.method == 'POST' and parsed.hostname == 'excashier.alipay.com'
                and parsed.path == '/standard/securityPost.json')

    def observe_payment_request(self, request):
        parsed = urlparse(request.url)
        if parsed.hostname == 'excashier.alipay.com' and parsed.path == '/standard/securityRender.phtm':
            self.record('alipay_account_validation_requested', account_validation_requested=True)
        if self.submitted and self.is_payment_request(request):
            self.record('payment_submitted_unknown', payment_request_observed=True,
                        payment_request_path=parsed.path)

    def verify_alipay(self, page):
        require_origin(page, 'excashier.alipay.com')
        text = page.locator('body').inner_text()
        if not re.search(r'收款方\s*[:：]\s*北京航空航天大学(?:\s|$)', text):
            raise PaymentError('支付宝收款方不是北京航空航天大学，停止付款。')
        if amount(page.locator('#J_basePriceArea strong').inner_text()) != self.expected.fee:
            raise PaymentError('支付宝显示金额与场馆订单金额不一致。')
        if not re.search(r'支付密码[，,]?\s*不是登录密码', text):
            raise PaymentError('支付宝账户付款表单发生变化，停止付款。')
        form = page.locator('#J_TloginForm')
        action = urlparse(form.get_attribute('action') or '')
        if (action.scheme != 'https' or action.netloc not in {'excashier.alipay.com', 'excashier.alipay.com:443'}
                or action.path != '/standard/securityPost.json'
                or (form.get_attribute('method') or '').lower() != 'post'):
            raise PaymentError('支付宝账户付款表单目标异常。')

    def verify_final_cashier(self, page):
        require_origin(page, 'cashier.alipay.com')
        if urlparse(page.url).path != '/business/cashiermain.htm':
            raise PaymentError('支付宝最终收银台地址发生变化。')
        label = page.get_by_text(f'支付{self.expected.fee:.2f}元', exact=True)
        page.get_by_text(re.compile(r'^支付\d+(?:\.\d{1,2})?元$')).wait_for(timeout=self.timeout(5000))
        text = page.locator('body').inner_text()
        if not re.search(r'订单名称\s*[:：]\s*场馆预约-羽毛球', text):
            raise PaymentError('支付宝最终收银台订单名称不一致。')
        match = re.search(r'订单金额\s*[:：]?\s*(\d+(?:\.\d{1,2})?)', text)
        if not match or amount(match[1]) != self.expected.fee:
            raise PaymentError('支付宝最终收银台金额不一致。')
        if self.charge and (not self.settings.account or not re.search(
                r'支付宝账户\s*[:：]\s*' + re.escape(self.settings.account) + r'(?:\s|$)', text)):
            raise PaymentError('支付宝最终收银台账户不一致。')
        if label.count() != 1 or not label.is_visible():
            raise PaymentError('支付宝最终付款金额标签不一致。')
        frames = [frame for frame in page.frames
                  if urlparse(frame.url).scheme == 'https'
                  and urlparse(frame.url).netloc == 'securitycore.alipay.com'
                  and urlparse(frame.url).path == '/validate.htm']
        if len(frames) != 1:
            raise PaymentError('未找到唯一的支付宝安全确认表单。')
        password = frames[0].locator('#payPassword_rsainput:visible')
        password.wait_for(timeout=self.timeout(5000))
        button = frames[0].locator('input#validateButton[type=button]:visible')
        if button.count() != 1 or button.input_value() != '确定':
            raise PaymentError('支付宝安全确认按钮发生变化。')
        return password, button

    def confirm_final_cashier(self, page):
        if not self.charge:
            raise PaymentError('付款预览不能激活最终付款按钮。')
        if self.final_charge_submitted:
            raise PaymentError('已激活最终付款按钮，不重复付款。')
        password, button = self.verify_final_cashier(page)
        password.fill(self.settings.password)
        self.expected.verify(self.read_order())
        if password.input_value() != self.settings.password:
            raise PaymentError('支付宝安全确认密码框发生变化，未点击确定。')
        self.final_charge_submitted = True
        self.submitted = True
        self.record('payment_submitted_unknown', final_charge_submitted=True,
                    final_confirmation_host='securitycore.alipay.com')
        # 调用正式按钮事件，仍由支付宝自身完成银行卡验证与实际付款。
        button.evaluate('button => button.click()', timeout=self.timeout(5000))

    def run(self):
        if self.started:
            raise PaymentError('本次订单已经尝试过付款流程，不重复提交。')
        self.started = True
        self.result_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.result_path.with_name(self.result_path.name + '.lock')
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise PaymentError('付款流程正在执行，或上次异常中断；请先核对记录，不并发付款。') from None
        os.close(fd)
        self.context.on('request', self.observe_payment_request)
        try:
            check_previous_result(self.result_path, self.expected.trade_no)
            return self._run()
        finally:
            self.context.remove_listener('request', self.observe_payment_request)
            lock.unlink()

    def _run(self):
        try:
            if self.expected.fee > self.settings.limit:
                raise PaymentError('订单价格超过单笔付款上限。')
            if self.charge and (not self.settings.account or not self.settings.password):
                raise PaymentError('缺少支付宝环境变量，未发起付款。')
            self.record('checking_order')
            alipay = self.open_alipay()
            if urlparse(alipay.url).hostname == 'cashier.alipay.com':
                self.verify_final_cashier(alipay)
                if not self.charge:
                    self.expected.verify(self.read_order())
                    self.record('payment_preview_ready')
                    return 'payment_preview_ready'
                self.confirm_final_cashier(alipay)
            else:
                self.verify_alipay(alipay)
                if not self.charge:
                    self.expected.verify(self.read_order())
                    self.record('payment_preview_ready')
                    return 'payment_preview_ready'
                # 不写浏览器 trace、截图或 HTML；字段填入后只点击一次正式按钮。
                account = alipay.locator('#J_tLoginId:visible')
                account.fill(self.settings.account)
                account.blur()
                # 只等待网站自身的账户校验和安全控件，不修改验证结果或触发私有提交方法。
                alipay.wait_for_function(ALIPAY_ACCOUNT_READY, timeout=self.timeout())
                self.record('alipay_account_ready')
                password = alipay.locator('#payPasswd_rsainput:visible')
                password.fill(self.settings.password)
                self.verify_alipay(alipay)
                self.expected.verify(self.read_order())
                if (account.input_value() != self.settings.account
                        or password.input_value() != self.settings.password):
                    raise PaymentError('支付宝页面更新导致输入内容变化，未点击付款；请检查页面状态。')
                alipay.wait_for_function(ALIPAY_ACCOUNT_READY, timeout=self.timeout())
                if self.deadline - time.monotonic() <= 5:
                    raise PaymentError('付款剩余时间不足，未点击付款。')
                # 先落盘；崩溃或超时时，结果按未知处理，不能自动重试。
                self.submitted = True
                self.record('payment_submitted_unknown', payer_form_submitted=True)
                try:
                    with self.context.expect_event('request', predicate=self.is_payment_request, timeout=self.timeout(5000)):
                        # 此收银台的指针点击可能不分发事件。只激活一次正式按钮，
                        # 由网站原有 click 处理器运行密码控件、验证和提交流程。
                        alipay.locator('#J_newBtn:visible').evaluate('button => button.click()', timeout=self.timeout(10000))
                except BrowserTimeout:
                    raise PaymentError('点击后未观察到支付宝付款验证请求，未再次点击；请核对页面和账单。') from None
            deadline = min(time.monotonic() + 30, self.deadline)
            while time.monotonic() < deadline:
                data = self.read_order()
                self.expected.verify(data, payable=False)
                if str(data.get('payStatus')) == '2':
                    self.record('paid', pay_status='2', order_status='1')
                    return 'paid'
                if str(data.get('payStatus')) != '1':
                    raise PaymentError('付款状态发生变化，请本人核对账单，不重试。')
                for next_page in self.context.pages:
                    if (urlparse(next_page.url).hostname == 'cashier.alipay.com'
                            and urlparse(next_page.url).path == '/business/cashiermain.htm'
                            and not self.final_charge_submitted):
                        self.confirm_final_cashier(next_page)
                    if (urlparse(next_page.url).hostname in {'excashier.alipay.com', 'cashier.alipay.com'}
                            and next_page.locator('#J_authSubmit:visible').count()):
                        self.record('payment_submitted_unknown', next_step='alipay_confirmation_required')
                        raise PaymentError('已进入支付宝第二步确认页面，尚未自动确认；请核对页面。')
                self.venue_page.wait_for_timeout(2000)
            raise PaymentError('付款尚未确认，可能需要二次验证；请本人核对支付宝及预约记录，不自动重试。')
        except PaymentError as error:
            self.record('payment_submitted_unknown' if self.submitted else 'payment_needs_attention', message=str(error))
            raise
        except (BrowserError, ValueError, KeyError, TypeError):
            self.record('payment_submitted_unknown' if self.submitted else 'payment_needs_attention', message='网页交互或交易响应异常。')
            raise PaymentError('付款流程已停止。请检查付款结果记录；如已提交付款表单，先核对支付宝及场馆订单，不重复运行。') from None
