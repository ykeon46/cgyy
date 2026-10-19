"""北航场馆预约。默认只查询；提交订单、预览付款和自动付款均需显式参数。"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta
from getpass import getpass
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout
from playwright.sync_api import Page, expect, sync_playwright
from payment import AlipayPayment, ExpectedOrder, PaymentError, PaymentSettings, check_previous_result
from text_captcha import CaptchaError, TextCaptchaSolver
from login_state import DEFAULT_SESSION_STATE, LoginState

BASE_URL = "https://cgyy.buaa.edu.cn"
CHINA = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger("cgyy")
DEFAULT_CONFIG = Path(__file__).parent / "configs" / "default.yaml"


class ReservationError(Exception):
    """可以直接展示给用户的失败原因。"""


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if not isinstance(config, dict):
        raise ReservationError("配置文件必须是 YAML 对象。")
    hours = config.get("reserve_time", [])
    if (not isinstance(hours, list) or not hours
            or any(type(h) is not int or not 0 <= h <= 23 for h in hours)
            or hours != list(range(hours[0], hours[0] + len(hours)))):
        raise ReservationError("reserve_time 必须是连续递增的小时，例如 [14, 15]。")
    priority = config.get("priority", [])
    if (not isinstance(priority, list) or not priority
            or any(type(p) is not int or p < 1 for p in priority)
            or len(priority) != len(set(priority))):
        raise ReservationError("priority 必须是不重复的正整数场地编号。")
    if not isinstance(config.get("sso", {}), dict):
        raise ReservationError("sso 必须包含 user 和 passwd 字段。")
    if not isinstance(config.get("venue"), str) or not config["venue"].strip():
        raise ReservationError("请配置 venue，可用 --list-venues 查看完整场馆名称。")
    candidates = config.get("candidates", [config.get("candidate", 1)])
    if (not isinstance(candidates, list)
            or any(type(c) is not int or c < 1 for c in candidates)
            or len(set(candidates)) != len(candidates)):
        raise ReservationError("candidates 必须是不重复的正整数列表，或 []。")
    config["candidates"] = candidates
    return config


def target_date(config: dict, today: date | None = None) -> date:
    today = today or datetime.now(CHINA).date()
    try:
        result = date.fromisoformat(str(config["reserve_date"])) if config.get("reserve_date") else today + timedelta(days=int(config.get("days_ahead", 2)))
    except (TypeError, ValueError, OverflowError) as error:
        raise ReservationError("reserve_date 应为 YYYY-MM-DD，days_ahead 应为整数。") from error
    if result < today:
        raise ReservationError("预约日期不能早于北京时间今天。")
    return result


def sleep_until(when: datetime) -> None:
    while (remaining := (when - datetime.now(CHINA)).total_seconds()) > 0:
        time.sleep(min(remaining, 1))


def wait_until(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value)
    except ValueError as error:
        raise ReservationError("--at 格式：2026-10-20T07:00:00（北京时间）。") from error
    when = when.replace(tzinfo=CHINA) if when.tzinfo is None else when
    if when <= datetime.now(CHINA):
        raise ReservationError("--at 必须是未来时间。")
    LOG.info("目标时间 %s；提前两分钟登录并准备场馆页面。", when.isoformat())
    sleep_until(when - timedelta(minutes=2))
    return when


def read_credentials(config: dict) -> tuple[str, str]:
    sso = config.get("sso", {})
    user = os.getenv("BUAA_SSO_USER") or sso.get("user") or input("统一认证学号/工号：").strip()
    password = os.getenv("BUAA_SSO_PASSWORD") or sso.get("passwd")
    if not password or set(str(password)) == {"*"}:
        password = getpass("统一认证密码（不回显）：")
    if not user or not password:
        raise ReservationError("学号和密码不能为空。")
    return str(user), str(password)


def authenticated_client(browser, config: dict, user: str, password_loader, login_state: LoginState,
                         *, ignore_password_warning: bool = False):
    context = login_state.new_context(browser, user)
    try:
        page = context.new_page()
        page.set_default_timeout(60000)
        client = VenueClient(page, config)
        if login_state.loaded:
            if client.resume_login(user):
                login_state.save(context, user)
                return context, client
            # 过期、错误账号或网络校验失败时，清空上下文后再走正式登录。
            context.close()
            context = login_state.new_context(browser, user, restore=False)
            page = context.new_page()
            page.set_default_timeout(60000)
            client = VenueClient(page, config)
        password = password_loader()
        if not password:
            raise ReservationError('登录态缺失或已过期，请提供统一认证密码以重新登录。')
        client.login(user, password, ignore_password_warning)
        login_state.save(context, user)
        return context, client
    except Exception:
        context.close()
        raise


class VenueClient:
    def __init__(self, page: Page, config: dict):
        self.page = page
        self.config = config

    def resume_login(self, user: str) -> bool:
        started = time.monotonic()
        try:
            self.page.goto(BASE_URL + '/venue/venue-introduce', wait_until='domcontentloaded', timeout=15000)
            self.page.get_by_text('退出', exact=True).wait_for(timeout=5000)
            confirmed = self.check_session(user)
            if confirmed:
                self.dismiss_notices()
                LOG.info('已复用本机登录态，服务器校验通过（%.3f 秒）。', time.monotonic() - started)
                return True
        except BrowserError:
            pass
        LOG.info('缓存登录态未通过校验，将重新登录。')
        return False

    def check_session(self, user: str) -> bool:
        """只读核验当前页面登录态，不离开已准备的预约页面。"""
        try:
            return bool(self.page.evaluate('''user => new Promise(resolve => {
                const timer = setTimeout(() => resolve(false), 5000);
                const done = result => { clearTimeout(timer); resolve(result); };
                const element = [...document.querySelectorAll('*')].find(e =>
                    e.__vue__?.commonMethods?.emitAjax && e.__vue__?.userInfo?.username);
                const vm = element?.__vue__;
                if (!vm || String(vm.userInfo.username) !== user) return done(false);
                vm.commonMethods.emitAjax({path:'/api/buddies', notPopUp:true,
                    success:() => done(true), error:() => done(false)});
            })''', user))
        except BrowserError:
            return False

    def dismiss_notices(self) -> None:
        for _ in range(4):
            modal = self.page.locator(".ivu-modal-wrap:visible")
            if not modal.count():
                return
            text = modal.first.inner_text()
            if "运行维护" in text or "不支持预约支付" in text:
                LOG.warning("网站公告：%s", text.replace("\n", " "))
                button = modal.first.get_by_role("button", name="确定", exact=True)
                if button.count():
                    dialog = modal.first.element_handle()
                    button.click()
                    dialog.wait_for_element_state("hidden")
                    continue
            raise ReservationError("网站提示：" + text)

    def login(self, user: str, password: str, ignore_password_warning: bool = False) -> None:
        page = self.page
        page.goto(BASE_URL + "/venue/Login", wait_until="domcontentloaded")
        page.get_by_text("统一身份认证登录", exact=True).click()
        # 当前 iframe 用 id 标识，不能仅按 name 查找。
        frame = page.frame_locator("#loginIframe")
        frame.locator("#unPassword").fill(user)
        frame.locator("#pwPassword").fill(password)
        if frame.locator("#cptPassword").is_visible():
            raise ReservationError("统一认证要求验证码，请在网站完成验证后重试。")
        frame.locator(".submit-btn:visible").first.click(no_wait_after=True)
        deadline = time.monotonic() + 90
        skipped = False
        warning_logged = False
        while time.monotonic() < deadline:
            host = urlparse(page.url).hostname
            if host == "cgyy.buaa.edu.cn" and page.get_by_text("退出", exact=True).is_visible():
                LOG.info("统一认证登录成功。")
                self.dismiss_notices()
                return
            if host == "sso.buaa.edu.cn":
                text = page.locator("body").inner_text()
                skip = page.locator("#btnSkip")
                if skip.is_visible() or "Your password is not safe" in text or "密码不安全" in text:
                    if not ignore_password_warning:
                        raise ReservationError("统一认证提示密码不安全，需修改密码；如需使用网页的"
                                               "“本次忽略”，添加 --ignore-password-warning。")
                    if not skipped:
                        if not warning_logged:
                            LOG.warning("等待统一认证网页的“本次忽略”按钮倒计时结束。")
                            warning_logged = True
                        if skip.is_visible() and skip.is_enabled():
                            skip.click(no_wait_after=True)
                            skipped = True
                for current in page.frames:
                    if urlparse(current.url).hostname == "sso.buaa.edu.cn":
                        error = current.locator("#errPassword")
                        if error.count() and error.is_visible() and error.inner_text().strip():
                            raise ReservationError("统一认证登录失败：" + error.inner_text().strip())
            page.wait_for_timeout(250)
        raise ReservationError("登录超时，可能需要验证码、二次认证，或统一认证服务暂不可用。")

    def list_venues(self) -> list[str]:
        # 使用网页自身导航，避免每次查询都重新加载整套前端资源。
        on_list = urlparse(self.page.url).path.rstrip("/") == "/venue/venue-introduce"
        navigation = self.page.locator(".tabItem").get_by_text("场地预约", exact=True)
        if not on_list and navigation.count() and navigation.first.is_visible():
            navigation.first.evaluate('element => element.click()')
            self.page.wait_for_url(re.compile(r'/venue/venue-introduce(?:[?#]|$)'))
        elif not on_list:
            self.page.goto(BASE_URL + "/venue/venue-introduce", wait_until="domcontentloaded")
        cards = self.page.locator(".place-item")
        cards.first.wait_for()
        self.dismiss_notices()
        return [name.strip() for name in cards.locator("h5").all_inner_texts()]

    def open_venue(self, names: list[str] | None = None) -> None:
        names = self.list_venues() if names is None else names
        matches = [n for n in names if self.config["venue"].strip() in n]
        if len(matches) != 1:
            raise ReservationError(f"场馆名称匹配了 {len(matches)} 项，请用 --list-venues 查看完整名称。")
        heading = self.page.locator("h5").filter(has_text=re.compile(r"^\s*" + re.escape(matches[0]) + r"\s*$"))
        self.page.locator(".place-item").filter(has=heading).get_by_text("订场", exact=True).click()
        self.page.locator(".reservationStep1").wait_for()
        self.page.locator("#scrollTable .reserveBlock").first.wait_for()
        LOG.info("场馆：%s", matches[0])

    def choose_date(self, desired: date) -> None:
        dates = self.page.locator(".date_box > div")
        dates.first.wait_for()
        label = f"{desired.month}月{desired.day}日"
        target = dates.filter(has=self.page.locator("span").filter(has_text=re.compile("^" + re.escape(label) + "$")))
        if target.count() != 1:
            available = "; ".join(dates.all_inner_texts()).replace("\n", " ")
            raise ReservationError(f"{desired} 尚未开放或不在预约日期范围内。当前可选：{available}")
        if "active" not in (target.get_attribute("class") or "").split():
            with self.page.expect_response(lambda r: urlparse(r.url).path.endswith("/api/reservation/day/info")) as pending:
                target.click()
            payload = pending.value.json()
            if not pending.value.ok or not isinstance(payload, dict) or str(payload.get("code")) != "200":
                raise ReservationError("日期查询失败，请查看网站当前开放范围或重新登录。")
            expect(target).to_have_class(re.compile(r"\bactive\b"))
        self.page.locator("#scrollTable .reserveBlock").first.wait_for()
        LOG.info("日期：%s", desired)

    def refresh_availability(self) -> None:
        # 调用网页刷新按钮使用的同一读取方法，让网站更新日期范围、余票及订单 token。
        # 不复用开放前的余票或 token，不直接构造下单请求。
        with self.page.expect_response(lambda r: urlparse(r.url).path.endswith('/api/reservation/day/info')) as pending:
            found = self.page.locator('.reservationStep1').evaluate('''element => {
                let vm = element.__vue__;
                while (vm && typeof vm.getReserveDate !== 'function') vm = vm.$parent;
                if (!vm) return false;
                vm.getReserveDate(); return true;
            }''')
            if not found:
                raise ReservationError('未找到网页自身的余票刷新方法，请检查网页结构。')
        response = pending.value
        payload = response.json()
        if not response.ok or not isinstance(payload, dict) or str(payload.get('code')) != '200':
            raise ReservationError('余票刷新失败，未提交订单。')
        self.page.locator('#scrollTable .reserveBlock').first.wait_for()

    def prepare_booking(self, desired: date) -> None:
        self.open_venue()
        try:
            self.choose_date(desired)
        except ReservationError as error:
            if '尚未开放或不在预约日期范围内' not in str(error):
                raise
            LOG.info('目标日期尚未显示，开放后刷新网站日期范围。')
        self.prepare_form()

    def grid(self) -> list[dict]:
        # 只读取 DOM，不通过 Vue 私有对象修改预约数据。
        return self.page.locator("#scrollTable table").evaluate(r"""table => {
            const head = [...table.querySelectorAll('thead tr:first-child > td, thead tr:first-child > th')].map(e => e.innerText.trim());
            const slots = [], time = s => s.match(/(\d{2}:\d{2})\s*[-~～]\s*(\d{2}:\d{2})/);
            for (const [rowIndex, row] of [...table.querySelectorAll('tbody > tr')].entries()) {
                const cells = [...row.children];
                for (let column = 1; column < Math.min(cells.length, head.length); column++) {
                    const block = cells[column].querySelector('.reserveBlock');
                    if (!block) continue;
                    const columnTime = time(head[column]), range = columnTime || time(cells[0].innerText);
                    if (!range) continue;
                    slots.push({court: columnTime ? cells[0].innerText.trim() : head[column], start: range[1], end: range[2],
                        classes: [...block.classList], row: rowIndex, column, text: block.innerText.trim()});
                }
            }
            return slots;
        }""")

    def turn_page(self, forward: bool) -> bool:
        arrow = self.page.locator("#scrollTable .ivu-icon-ios-arrow-" + ("forward" if forward else "back"))
        if not arrow.count() or not arrow.first.is_visible():
            return False
        previous = self.page.locator("#scrollTable").inner_text()
        arrow.first.click()
        expect(self.page.locator("#scrollTable")).not_to_have_text(previous)
        return True

    def first_page(self) -> None:
        for _ in range(100):
            if not self.turn_page(False):
                return
        raise ReservationError("表格分页超过 100 页，请检查网页结构。")

    def inspect(self) -> list[dict]:
        self.first_page()
        slots = []
        for index in range(100):
            slots.extend(dict(cell, page=index) for cell in self.grid())
            if not self.turn_page(True):
                if not slots:
                    raise ReservationError("未读到场地时段，网页结构或开放状态可能已变化。")
                return slots
        raise ReservationError("表格分页超过 100 页，请检查网页结构。")

    def find_choice(self) -> list[dict]:
        """按页面顺序查找，找到满足连续时段的场地就停止翻页。"""
        self.first_page()
        slots = []
        for index in range(100):
            slots.extend(dict(cell, page=index) for cell in self.grid())
            chosen = available_choice(slots, self.config)
            if chosen:
                return chosen
            if not self.turn_page(True):
                return []
        raise ReservationError('表格分页超过 100 页，请检查网页结构。')

    def select(self, cells: list[dict]) -> None:
        self.first_page()
        page_index = 0
        for cell in sorted(cells, key=lambda c: c["page"]):
            while page_index < cell["page"]:
                if not self.turn_page(True):
                    raise ReservationError("表格发生变化，请重新查询。")
                page_index += 1
            fresh = next((c for c in self.grid() if c["court"] == cell["court"] and c["start"] == cell["start"] and c["end"] == cell["end"]), None)
            if fresh is None or "free" not in fresh["classes"]:
                raise ReservationError("目标场地已不可用，请重新查询。")
            block = self.page.locator("#scrollTable tbody > tr").nth(fresh["row"]).locator(":scope > td").nth(fresh["column"]).locator(".reserveBlock")
            block.click()
            expect(block).to_have_class(re.compile(r"\bselected\b"))

    def prepare_form(self) -> None:
        # 页面中的隐藏弹窗也有同名手机号输入框，只填写当前预约表单。
        phone = self.page.locator('.reservationStep1 input[placeholder="请输入手机号"]:visible')
        if not phone.count():
            phone = self.page.locator('input[placeholder="请输入手机号"]:visible')
        if phone.count() > 1:
            raise ReservationError("页面存在多个可见手机号输入框，请检查当前预约表单。")
        if phone.count() and not phone.input_value():
            if not self.config.get("phone"):
                raise ReservationError("页面要求手机号，请在配置中设置 phone。")
            phone.fill(str(self.config["phone"]))
        buddies = self.page.locator(".companion_box .buddy-row:not(.addBtn)")
        if self.page.locator(".companion_box").count():
            for index in self.config["candidates"]:
                if index > buddies.count():
                    raise ReservationError(f"未找到第 {index} 位同伴，请先在网站添加同伴或设置 candidates: []。")
                buddy = buddies.nth(index - 1)
                if "active" not in (buddy.get_attribute("class") or "").split():
                    buddy.click()
                expect(buddy).to_have_class(re.compile(r"\bactive\b"))
        else:
            group = self.page.locator(".ivu-form-item").filter(has=self.page.locator(".ivu-form-item-label").filter(has_text=re.compile(r"^\s*同伴\s*$")))
            boxes = group.locator("input[type=checkbox]")
            for index in self.config["candidates"] if group.count() else []:
                if index > boxes.count():
                    raise ReservationError(f"未找到第 {index} 位同伴，请先在网站添加同伴或设置 candidates: []。")
                boxes.nth(index - 1).check()
        agreement = self.page.locator(".xieyi input[type=checkbox]")
        if agreement.count():
            agreement.check()

    def submit(self, cells: list[dict], *, timeout: int = 15000, captcha_solver=None) -> dict:
        started = time.monotonic()
        deadline = started + timeout / 1000
        now = datetime.now(CHINA)
        if now.hour >= 23 or now.hour < 5:
            raise ReservationError("网站每日 23:00 至次日 05:00 维护，请在维护结束后提交。")
        self.select(cells)
        self.prepare_form()
        button = self.page.locator(".submit_order_box .action .btn").filter(has_text=re.compile(r"^\s*提交\s*$"))
        if button.count() != 1:
            raise ReservationError("未找到唯一提交按钮，请检查预约页面。")
        expect(button).not_to_have_class(re.compile(r"\bdisab\b"))
        received = []
        def observe(response):
            if urlparse(response.url).path.endswith('/api/reservation/order/submit'):
                received.append(response)
        self.page.on('response', observe)
        try:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise ReservationError('选场准备已超过下单时间预算，未点击提交。')
            # 不自动重试下单：超时也可能已经创建订单。
            with self.page.expect_response(lambda r: urlparse(r.url).path.endswith("/api/reservation/order/submit"), timeout=remaining) as pending:
                button.click()
                if captcha_solver:
                    while not received and time.monotonic() < deadline:
                        if self.page.locator('.verifybox:visible').count():
                            captcha_solver(self.page, timeout=max(1, int((deadline - time.monotonic()) * 1000)))
                            break
                        self.page.wait_for_timeout(100)
            response = pending.value
            payload = response.json()
        except BrowserTimeout as error:
            notices = self.page.locator(".ivu-message-notice-content:visible,.ivu-modal-wrap:visible").all_inner_texts()
            raise ReservationError("未收到订单响应，请在网页核对订单或完成验证；不自动重试。 " + " ".join(notices)) from error
        finally:
            self.page.remove_listener('response', observe)
        if not response.ok or not isinstance(payload, dict):
            raise ReservationError("订单响应异常，请在个人中心核对，避免重复提交。")
        data = payload.get("data")
        if str(payload.get("code")) != "200" or not isinstance(data, dict) or not data.get("tradeNo"):
            raise ReservationError("订单未确认成功：" + str(payload.get("message") or payload.get("msg") or "请查看网页提示"))
        LOG.info("订单已提交（选场、表单、验证码及提交共 %.3f 秒）。请按页面倒计时付款。", time.monotonic() - started)
        return data


def available_choice(slots: list[dict], config: dict) -> list[dict]:
    # priority 是偏好顺序；副馆实际从 17 号起，不能只检查主馆的 1–12 号。
    numbered = {}
    for slot in slots:
        match = re.fullmatch(r"\s*(\d+)\s*(?:号|号场|号场地)?\s*", slot["court"])
        if match:
            numbered.setdefault(int(match.group(1)), set()).add(slot["court"])
    order = list(dict.fromkeys([*config["priority"], *sorted(numbered)]))
    for number in order:
        labels = numbered.get(number, set())
        if len(labels) != 1:
            continue
        chosen = []
        for hour in config["reserve_time"]:
            matches = [s for s in slots if s["court"] in labels and s["start"] == f"{hour:02}:00" and s["end"] == f"{hour + 1:02}:00" and "free" in s["classes"]]
            if len(matches) != 1:
                break
            chosen.append(matches[0])
        else:
            return chosen
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-login", action="store_true", help="仅检查统一认证登录")
    mode.add_argument("--list-venues", action="store_true", help="列出当前场馆名称")
    mode.add_argument("--submit", action="store_true", help="实际提交订单；默认手动付款")
    payment_mode = parser.add_mutually_exclusive_group()
    payment_mode.add_argument("--auto-pay", action="store_true", help="创建订单后用支付宝环境变量提交付款，可能实际扣款")
    payment_mode.add_argument("--payment-preview", action="store_true", help="创建订单并打开支付宝账户付款页，不填写凭据、不提交付款")
    parser.add_argument("--max-pay-amount", help="人民币单笔付款上限，覆盖 CGYY_MAX_PAY_AMOUNT")
    parser.add_argument("--payment-result", type=Path, default=Path('artifacts/payment-result.json'), help="付款状态记录（不含凭据）")
    parser.add_argument("--date", help="预约日期 YYYY-MM-DD，覆盖配置")
    parser.add_argument("--at", help="指定北京时间开始，例如 2026-10-20T07:00:00")
    parser.add_argument("--headless", action="store_true", help="无界面浏览器；提交需同时指定付款预览或自动付款")
    parser.add_argument('--fast', action='store_true', help='找到可用场地即停止翻页；开放前准备页面，开放后以10秒为下单预算')
    parser.add_argument('--session-state', type=Path, default=DEFAULT_SESSION_STATE, help='本机北航登录态缓存（Cookie 和 localStorage）')
    parser.add_argument('--no-session-cache', action='store_true', help='禁用本机登录态缓存，每次重新登录')
    parser.add_argument("--browser-binary", default=os.getenv("CGYY_BROWSER_BINARY"), help="自定义 Chromium/Chrome 可执行文件")
    parser.add_argument("--proxy", help="浏览器代理，默认读取 HTTPS_PROXY/HTTP_PROXY")
    parser.add_argument("--ignore-password-warning", action="store_true", help="使用统一认证网页的“本次忽略”按钮")
    parser.add_argument('--captcha-repo', type=Path, help='本地 Text_select_captcha 仓库路径，显式启用下单验证码识别')
    parser.add_argument('--captcha-font', type=Path, default=Path('/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'), help='验证码提示字使用的中文 TrueType 字体')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        payment_enabled = args.auto_pay or args.payment_preview
        if payment_enabled and not args.submit:
            raise ReservationError('--auto-pay / --payment-preview 必须与 --submit 一起使用。')
        if args.submit and args.headless and not payment_enabled:
            raise ReservationError("--submit 请使用有界面浏览器，以便核对订单并手动付款。")
        payment_settings = PaymentSettings.from_env(args.max_pay_amount, charge=args.auto_pay) if payment_enabled else None
        if payment_enabled:
            check_previous_result(args.payment_result)
        config = load_config(args.config)
        captcha_solver = TextCaptchaSolver(args.captcha_repo, args.captcha_font) if args.captcha_repo else None
        if args.date:
            config["reserve_date"] = args.date
        start = wait_until(args.at) if args.at else None
        today = start.astimezone(CHINA).date() if start else None
        desired = None if args.check_login or args.list_venues else target_date(config, today)
        user = str(os.getenv('BUAA_SSO_USER') or config.get('sso', {}).get('user') or input('统一认证学号/工号：').strip())
        if not user:
            raise ReservationError('学号和工号不能为空。')
        login_state = LoginState(None if args.no_session_cache else args.session_state)
        with sync_playwright() as playwright:
            options = {"headless": args.headless}
            if args.browser_binary:
                options["executable_path"] = args.browser_binary
            proxy = args.proxy or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
            if proxy:
                options["proxy"] = {"server": proxy}
            browser = playwright.chromium.launch(**options)
            try:
                context, client = authenticated_client(browser, config, user,
                    lambda: read_credentials(dict(config, sso=dict(config.get('sso', {}), user=user)))[1],
                    login_state, ignore_password_warning=args.ignore_password_warning)
                page = client.page
                if args.check_login:
                    return 0
                if args.list_venues:
                    print("\n".join(client.list_venues()))
                    return 0
                if start:
                    client.prepare_booking(desired)
                    sleep_until(start)
                booking_started = time.monotonic()
                if start:
                    client.refresh_availability()
                else:
                    client.open_venue()
                client.choose_date(desired)
                slots = client.find_choice() if args.fast else client.inspect()
                for cell in slots:
                    if cell["start"] in {f"{h:02}:00" for h in config["reserve_time"]}:
                        status = "空闲" if "free" in cell["classes"] else "不可用"
                        print(f'{cell["court"]:8} {cell["start"]}-{cell["end"]} {status} {cell["text"]}')
                chosen = available_choice(slots, config)
                if not chosen:
                    LOG.info("没有满足连续时段和场地优先级的空闲场地。")
                    return 3
                LOG.info("可选：%s，%s 至 %s", chosen[0]["court"], chosen[0]["start"], chosen[-1]["end"])
                if args.submit:
                    if payment_enabled:
                        expected = ExpectedOrder.from_choice('', config['venue'], desired, chosen, payment_settings.limit)
                        budget = max(1, int((10 - (time.monotonic() - booking_started)) * 1000)) if args.fast else (45000 if captcha_solver else (15000 if args.headless else 120000))
                        if args.fast and time.monotonic() - booking_started >= 10:
                            raise ReservationError('查询已超过10秒预算，未提交订单。')
                        order = client.submit(chosen, timeout=budget, captcha_solver=captcha_solver)
                        LOG.info('本轮查询至订单确认耗时 %.3f 秒。', time.monotonic() - booking_started)
                        expected = replace(expected, trade_no=order['tradeNo'])
                        status = AlipayPayment(page, expected, payment_settings, args.payment_result, charge=args.auto_pay).run()
                        LOG.info('付款流程状态：%s；记录：%s', status, args.payment_result)
                        if not args.headless and not args.auto_pay:
                            input('支付宝付款页已打开，未填写凭据或付款。按回车关闭浏览器：')
                    else:
                        try:
                            budget = max(1, int((10 - (time.monotonic() - booking_started)) * 1000)) if args.fast else (45000 if captcha_solver else 15000)
                            if args.fast and time.monotonic() - booking_started >= 10:
                                raise ReservationError('查询已超过10秒预算，未提交订单。')
                            client.submit(chosen, timeout=budget, captcha_solver=captcha_solver)
                            LOG.info('本轮查询至订单确认耗时 %.3f 秒。', time.monotonic() - booking_started)
                        finally:
                            input("请在浏览器核对订单并按需付款，完成后按回车关闭浏览器：")
                else:
                    LOG.info("只查询模式，未选择场地或提交订单。实际预约使用 --submit。")
            finally:
                browser.close()
        return 0
    except (ReservationError, PaymentError, CaptchaError, OSError, yaml.YAMLError) as error:
        LOG.error("%s", error)
        return 1
    except BrowserError:
        # 原始浏览器异常可能包含登录票据或填写内容，不输出其调用日志。
        LOG.error("浏览器操作失败：请检查网络、网页提示和浏览器安装（python -m playwright install chromium）。")
        return 2
    except (KeyboardInterrupt, EOFError):
        LOG.info("已退出。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
