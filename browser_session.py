"""常驻浏览器会话：同一个浏览器和学校登录态供登录、预约、核对订单复用。

学校页面加载慢，每次新开浏览器再恢复登录态要十几秒。会话在最近一次成功请求后
短时间内直接复用，超过后只发一个只读请求核验，失效时才恢复缓存登录态或重新登录。
"""
from __future__ import annotations

import time

from playwright.sync_api import Error as BrowserError

from cgyy import LOG, authenticated_client, on_reservation_page, open_venue_tabs
from login_state import LoginState

FRESH_SECONDS = 60


class BrowserSession:
    def __init__(self, browser):
        self.browser = browser
        self.user = None
        self.context = None
        self.client = None
        self.verified_at = 0.0
        self.stale = False
        # 空闲时预先打开的场馆预约页：{场馆: VenueClient}。第一个场馆使用登录页面本身。
        self.tabs = {}

    def fresh(self) -> bool:
        return not self.stale and time.monotonic() - self.verified_at < FRESH_SECONDS

    def mark_verified(self) -> None:
        self.verified_at = time.monotonic()
        self.stale = False

    def mark_invalid(self) -> None:
        self.stale = True

    def close_tabs(self) -> None:
        for client in self.tabs.values():
            if self.client is None or client.page is not self.client.page:
                try:
                    client.page.close()
                except BrowserError:
                    pass
        self.tabs = {}

    def take_tabs(self, venues: list[str]) -> dict:
        """交出与所需场馆一致、仍停在预约页的标签页；不一致时全部关闭，返回空字典。"""
        tabs, self.tabs = self.tabs, {}
        usable = (self.client is not None and list(tabs) == list(venues)
                  and tabs[venues[0]].page is self.client.page
                  and all(on_reservation_page(client) for client in tabs.values()))
        if not usable:
            self.tabs = tabs
            self.close_tabs()
            return {}
        return tabs

    def keep_tabs(self, tabs: dict) -> None:
        """预约结束后收回标签页，下一次预约继续复用；已离开预约页的会在预热时替换。"""
        self.close_tabs()
        if self.client is not None and tabs and next(iter(tabs.values())).page is self.client.page:
            self.tabs = dict(tabs)
        else:
            for client in tabs.values():
                if self.client is None or client.page is not self.client.page:
                    try:
                        client.page.close()
                    except BrowserError:
                        pass

    def warm_up(self, config: dict, desired) -> bool:
        """空闲时把所有场馆的预约页打开到目标日期。只发出一轮正常的页面请求，之后不再轮询。"""
        if self.client is None or self.stale:
            return False
        venues = config['venues']
        if list(self.tabs) == list(venues) and all(on_reservation_page(c) for c in self.tabs.values()):
            return True
        self.close_tabs()
        started = time.monotonic()
        tabs, ready = open_venue_tabs(self.client, config, desired)
        self.tabs = tabs
        if ready != set(venues):
            self.close_tabs()
            return False
        LOG.info('已预先打开%d个场馆的预约页（%.1f 秒）。', len(tabs), time.monotonic() - started)
        return True

    def close(self) -> None:
        self.tabs = {}
        if self.context is not None:
            try:
                self.context.close()
            except BrowserError:
                pass
        self.user = self.context = self.client = None
        self.stale = False

    def adopt(self, user: str, context, client) -> None:
        if context is not self.context:
            self.close()
        self.user, self.context, self.client = user, context, client
        self.mark_verified()

    def acquire(self, user: str, password: str, *, config=None, login_state=None, progress=None):
        """返回已登录的 (context, client)。调用方不得关闭这个 context。"""
        if self.client is not None and self.user == user and not self.stale:
            if self.fresh() or self.client.check_session(user):
                self.mark_verified()
                self.client.config = dict(config or {})
                return self.context, self.client
        self.close()
        context, client = authenticated_client(self.browser, dict(config or {}), user, lambda: password,
                                               login_state or LoginState(), ignore_password_warning=True,
                                               progress=progress)
        self.adopt(user, context, client)
        return context, client

    def keep_alive(self, login_state=None) -> None:
        """空闲时核验一次登录态并刷新本机缓存；失效只做标记，下次使用时再登录。"""
        if self.client is None or self.stale:
            return
        if self.client.check_session(self.user):
            self.mark_verified()
            (login_state or LoginState()).save(self.context, self.user)
        else:
            self.mark_invalid()
