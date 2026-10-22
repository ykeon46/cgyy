const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const constants = require('../miniprogram/utils/constants');

function harness() {
  const clock = {now: Date.parse('2026-10-22T06:00:00+08:00')};
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }
  let storage = {server: 'https://test.invalid', key: 'k'.repeat(24), token: 'test-token'};
  const calls = [], intervals = new Map(), app = {globalData: {currentId: null}};
  let intervalId = 0;
  const wx = {
    getStorageSync() { return storage; }, setStorageSync(key, value) { storage = value; },
    vibrateShort() {}, pageScrollTo() {}, showToast() {}, reLaunch() {},
    stopPullDownRefresh() {}, showModal(options) { options.success({confirm: true}); }
  };
  const apiModule = {exports: {}};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'miniprogram/utils/api.js'), 'utf8'), {
    wx, Date: ClockDate, module: apiModule
  });
  const api = apiModule.exports;
  const config = {
    venues: constants.HALLS, reserve_date: null, days_ahead: 2, reserve_time: [20, 21],
    priority: constants.COURTS, candidates: [1], phone: '', max_pay_amount: '70',
    daily_schedule: {login_at: '06:05:00', release_at: '07:00:00', stop_at: '08:00:00', keep_alive_seconds: 300}
  };
  const state = {jobs: [], config, daily_enabled: false};
  api.request = async (url, body) => {
    calls.push({url, body});
    if (url === '/api/session') return {session: {username: 'test-user', buddies: [{index: 1, name: '同伴'}]}, server_now: new ClockDate().toISOString()};
    if (url === '/api/config' && body === undefined) return {config: state.config, daily_enabled: state.daily_enabled};
    if (url === '/api/jobs' && body === undefined) return {jobs: state.jobs, server_now: new ClockDate().toISOString()};
    return {ok: true, id: 'created-job'};
  };
  let definition;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'miniprogram/pages/index/index.js'), 'utf8'), {
    require(name) { return name.endsWith('/api') ? api : constants; },
    getApp() { return app; }, Page(value) { definition = value; }, wx, Date: ClockDate,
    setInterval(fn) { intervals.set(++intervalId, fn); return intervalId; },
    clearInterval(id) { intervals.delete(id); }
  });
  const page = Object.assign({}, definition, {data: JSON.parse(JSON.stringify(definition.data))});
  page.setData = function(values) {
    for (const [key, value] of Object.entries(values)) {
      const keys = key.replace(/\[(\d+)\]/g, '.$1').split('.');
      let target = this.data;
      for (const part of keys.slice(0, -1)) target = target[part];
      target[keys[keys.length - 1]] = value;
    }
  };
  page.onLoad();
  return {page, api, app, wx, state, calls, clock, intervals};
}

test('new payment QR overrides pinned history and clears expired QR', () => {
  const {page, app, clock} = harness();
  app.globalData.currentId = 'old';
  page.jobs = [
    {id: 'new', status: 'awaiting_payment', qr_code: 'data:image/png;base64,eA==', expires_at: new Date(clock.now + 45000).toISOString()},
    {id: 'old', status: 'paid'}
  ];
  page.renderPayment();
  assert.equal(page.data.pay.id, 'new');
  assert.equal(page.data.live, true);
  assert.equal(page.data.pinned, false);
  clock.now += 46000;
  page.updateCountdown();
  assert.equal(page.data.qr, '');
  assert.equal(page.data.live, false);
});

test('manual bookings follow latest instead of leaving a permanent pin', async () => {
  const {page, app, calls} = harness();
  await page.load();
  await page.refreshJobs();
  app.globalData.currentId = 'old';
  await page.onSubmit();
  assert.equal(app.globalData.currentId, null);
  const request = calls.find(call => call.url === '/api/jobs' && call.body);
  assert.deepEqual(Array.from(request.body.config.reserve_time), [20, 21]);
});

test('submit and save do nothing before configuration and job state load', async () => {
  const {page, calls} = harness();
  await page.onSubmit();
  await page.onSave();
  assert.equal(calls.length, 0);
  assert.throws(() => page.readConfig(), /配置尚未加载/);
  await page.load();
  await page.onSubmit();
  assert.equal(calls.some(call => call.body), false);
});

test('configuration failure keeps submission disabled until successful retry', async () => {
  const {page, api, calls} = harness();
  const request = api.request;
  api.request = async (url, body) => {
    if (url === '/api/config') throw new Error('config unavailable');
    return request(url, body);
  };
  await assert.rejects(page.load(), /config unavailable/);
  assert.equal(page.data.ready, false);
  assert.equal(page.data.loading, false);
  await page.onSubmit();
  assert.equal(calls.some(call => call.body), false);
  api.request = request;
  await page.load();
  assert.equal(page.data.ready, true);
});

test('returning to the page revalidates config while preserving unsaved edits', async () => {
  const {page, calls, intervals} = harness();
  page.onShow();
  await page.loading;
  page.data.phone = '13800000001';
  page.onHide();
  page.onShow();
  await page.loading;
  assert.equal(calls.filter(call => call.url === '/api/config').length, 2);
  assert.equal(page.data.phone, '13800000001');
  assert.equal(intervals.size, 2);
});

test('server configuration changes are synchronized when returning', async () => {
  const {page, state} = harness();
  await page.load();
  state.config = {...state.config, reserve_time: [19, 20]};
  state.daily_enabled = true;
  await page.load();
  assert.equal(page.data.startLabels[page.data.startIndex], '19:00');
  assert.equal(page.data.dailyEnabled, true);
});

test('rolling booking and default scheduled dates advance across midnight', async () => {
  const {page, clock} = harness();
  await page.load();
  assert.equal(page.data.reserveDate, '2026-10-24');
  assert.equal(page.data.runDate, '2026-10-22');
  clock.now += 86400000;
  await page.load();
  assert.equal(page.data.dateStart, '2026-10-23');
  assert.equal(page.data.dateEnd, '2026-10-26');
  assert.equal(page.data.reserveDate, '2026-10-25');
  assert.equal(page.data.runDate, '2026-10-23');
});

test('explicit future dates survive revalidation and midnight rollover', async () => {
  const {page, clock} = harness();
  await page.load();
  page.onDate({detail: {value: '2026-10-24'}});
  page.onRunDate({detail: {value: '2026-10-24'}});
  clock.now += 86400000;
  await page.load();
  assert.equal(page.data.reserveDate, '2026-10-24');
  assert.equal(page.data.runDate, '2026-10-24');
});

test('default scheduled date advances when today’s release time has passed', async () => {
  const {page, clock} = harness();
  await page.load();
  assert.equal(page.data.runDate, '2026-10-22');
  clock.now += 2 * 3600000;
  await page.load();
  assert.equal(page.data.runDate, '2026-10-23');
});

test('daily rules save a rolling day offset instead of a stale fixed date', async () => {
  const {page} = harness();
  await page.load();
  page.setMode({currentTarget: {dataset: {mode: 'daily'}}});
  assert.equal(page.readConfig().reserve_date, null);
  assert.equal(page.readConfig().days_ahead, 2);
});

test('poll failures mark state stale, block submission, and recover on success', async () => {
  const {page, api} = harness();
  await page.load();
  await page.refreshJobs();
  const request = api.request;
  api.request = async () => { throw new Error('offline'); };
  await page.refreshJobs();
  assert.match(page.data.networkError, /连接中断/);
  assert.equal(page.data.jobsReady, false);
  api.request = request;
  await page.refreshJobs();
  assert.equal(page.data.networkError, '');
  assert.equal(page.data.jobsReady, true);
});

test('unknown submissions expose manual resolution only without an order number', () => {
  const {page} = harness();
  page.jobs = [{id: 'unknown', status: 'needs_attention'}];
  page.renderPayment();
  assert.equal(page.data.blocked, true);
  assert.equal(page.data.pay.resolvable, true);
  page.jobs[0].trade_no = 'known-order';
  page.renderPayment();
  assert.equal(page.data.pay.resolvable, false);
});

test('resolution sends explicit confirmation and never sends on cancel', async () => {
  const {page, calls, wx, state} = harness();
  page.jobs = state.jobs = [{id: 'unknown', status: 'needs_attention'}];
  page.renderPayment();
  wx.showModal = options => options.success({confirm: false});
  page.resolveJob();
  assert.equal(calls.length, 0);
  wx.showModal = options => options.success({confirm: true});
  page.resolveJob();
  await Promise.resolve();
  const request = calls.find(call => call.url === '/api/resolve');
  assert.equal(request.body.id, 'unknown');
  assert.equal(request.body.confirmed_no_order, true);
});
