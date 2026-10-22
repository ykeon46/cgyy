const api = require('../../utils/api');
const {HALLS, COURTS, LABELS, RUNNING, venueName} = require('../../utils/constants');

const DAYS = ['当天', '1天', '2天', '3天'];
const KEEP_ALIVE = [60, 180, 300, 600];
const START_HOURS = Array.from({length: 15}, (_, i) => i + 7);
const END_HOURS = Array.from({length: 15}, (_, i) => i + 8);
const hourLabel = hour => String(hour).padStart(2, '0') + ':00';
const MODES = {
  now: {submit: '预约并获取付款码', hint: '查询一次所选场馆，有空场立即下单并显示付款码；无空场直接返回。'},
  once: {submit: '创建定时预约', hint: '到点前约2分钟准备登录，开放后持续查找，最多5分钟。'},
  daily: {submit: '保存每日规则', hint: '每天按提前天数滚动预约，找到连续场地后只提交一笔订单。'}
};
const app = getApp();

Page({
  data: {
    username: '', today: '',
    halls: [
      {name: HALLS[0], title: '体育馆主馆', sub: '学院路 · 1–12号场地', checked: true},
      {name: HALLS[1], title: '训练馆 / 副馆', sub: '学院路 · 17–24号场地', checked: true}
    ],
    days: DAYS, daysIndex: 2, reserveDate: '', dateStart: '', dateEnd: '',
    startLabels: START_HOURS.map(hourLabel), endLabels: END_HOURS.map(hourLabel), startIndex: 13, endIndex: 14, duration: '2小时',
    courts: COURTS.map(n => ({n, selected: false})),
    mode: 'now', submitText: MODES.now.submit, modeHint: MODES.now.hint, pinned: false,
    runDate: '', runTime: '07:00',
    loginAt: '06:05', releaseAt: '07:00', stopAt: '08:00', dailyEnabled: false,
    keepAliveLabels: ['每1分钟', '每3分钟', '每5分钟', '每10分钟'], keepAliveIndex: 2,
    buddies: [], phone: '', maxAmount: '70', fast: true, remember: true, advanced: false,
    formError: '', networkError: '', busy: false, blocked: false, ready: false, jobsReady: false, loading: false,
    pay: null, qr: '', countdown: '00:00', progress: 0, live: false
  },

  onLoad() {
    this.serverOffset = 0;
    this.jobs = [];
    this.lastPaid = null;
  },

  onShow() {
    if (!api.connection().token) return api.goLogin();
    this.stop();
    this.setData({ready: false, jobsReady: false});
    this.load().catch(error => this.handleError(error));
    this.refreshJobs();
    this.poll = setInterval(() => this.refreshJobs(), 1500);
    this.tick = setInterval(() => this.updateCountdown(), 500);
  },

  onHide() { this.stop(); },
  onUnload() { this.stop(); },
  stop() { clearInterval(this.poll); clearInterval(this.tick); },

  onPullDownRefresh() {
    Promise.all([this.load(), this.refreshJobs()]).catch(error => this.handleError(error)).then(() => wx.stopPullDownRefresh());
  },

  handleError(error) {
    if (error.status === 401) { this.stop(); return api.goLogin(); }
    this.setData({formError: error.message});
  },

  async load() {
    if (this.loading) return this.loading;
    this.loading = this.loadState();
    try { await this.loading; } finally { this.loading = null; }
  },

  async loadState() {
    this.setData({ready: false, loading: true});
    try {
      const state = await api.request('/api/session');
      if (!state.session) { api.saveConnection({token: ''}); this.stop(); return api.goLogin(); }
      this.serverOffset = api.parseTime(state.server_now) - Date.now();
      const session = state.session;
      const buddies = (session.buddies || []).map(b => ({index: b.index, name: b.name, checked: this.data.buddies.some(old => old.index === b.index && old.checked)}));
      this.setData({username: session.username, buddies});
      const data = await api.request('/api/config');
      const snapshot = JSON.stringify(data);
      // 回到页面时核验服务器配置，但保留尚未保存的表单修改。
      if (!this.loaded || snapshot !== this.configSnapshot) this.fillConfig(data.config, data.daily_enabled);
      this.configSnapshot = snapshot;
      this.syncDates();
      this.loaded = true;
      this.setData({ready: true, formError: ''});
    } finally { this.setData({loading: false}); }
  },

  syncDates() {
    const today = api.beijingDate(this.serverOffset);
    const end = api.beijingDate(this.serverOffset, 3);
    const crossedDay = this.calendarDay && this.calendarDay !== today;
    const update = {today: today.slice(5, 10).replace('-', '月') + '日', dateStart: today, dateEnd: end};
    if (!this.data.reserveDate || (crossedDay && this.reserveDateRolling) || this.data.reserveDate < today || this.data.reserveDate > end) {
      update.reserveDate = api.beijingDate(this.serverOffset, this.data.daysIndex);
      this.reserveDateRolling = true;
    }
    const now = new Date(Date.now() + this.serverOffset + 8 * 3600000).toISOString().slice(11, 16);
    const defaultRunElapsed = !this.runDateTouched && this.data.runDate === today && now >= this.data.runTime;
    if (!this.data.runDate || defaultRunElapsed || (crossedDay && !this.runDateTouched) || this.data.runDate < today) {
      update.runDate = api.beijingDate(this.serverOffset, now >= this.data.runTime ? 1 : 0);
    }
    this.calendarDay = today;
    this.setData(update);
  },

  fillConfig(c, dailyEnabled) {
    const venues = c.venues || HALLS;
    const priority = c.priority && c.priority.length < 20 ? c.priority : [];
    const days = c.days_ahead === undefined || c.days_ahead === null ? 2 : c.days_ahead;
    const today = api.beijingDate(this.serverOffset);
    const schedule = c.daily_schedule || {};
    this.reserveDateRolling = !c.reserve_date || c.reserve_date === api.beijingDate(this.serverOffset, days);
    this.setData({
      halls: this.data.halls.map(h => Object.assign({}, h, {checked: venues.includes(h.name)})),
      daysIndex: days,
      reserveDate: c.reserve_date && c.reserve_date >= today ? c.reserve_date : api.beijingDate(this.serverOffset, days),
      courts: COURTS.map(n => ({n, selected: priority.includes(n)})),
      startIndex: START_HOURS.indexOf(c.reserve_time[0]),
      endIndex: END_HOURS.indexOf(c.reserve_time[c.reserve_time.length - 1] + 1),
      buddies: this.data.buddies.map(b => Object.assign({}, b, {checked: (c.candidates || []).includes(b.index)})),
      phone: c.phone || '', maxAmount: String(c.max_pay_amount || 70),
      keepAliveIndex: Math.max(0, KEEP_ALIVE.indexOf(schedule.keep_alive_seconds || 300)),
      loginAt: (schedule.login_at || '06:05').slice(0, 5),
      releaseAt: (schedule.release_at || '07:00').slice(0, 5),
      stopAt: (schedule.stop_at || '08:00').slice(0, 5),
      fast: c.fast !== false, remember: c.remember_session !== false, dailyEnabled: !!dailyEnabled
    });
    this.updateDuration();
  },

  readConfig() {
    if (!this.data.ready) throw new Error('配置尚未加载成功，请下拉刷新后重试。');
    this.syncDates();
    const d = this.data;
    const start = START_HOURS[d.startIndex], end = END_HOURS[d.endIndex];
    if (end <= start) throw new Error('结束时间须晚于开始时间。');
    const venues = d.halls.filter(h => h.checked).map(h => h.name);
    if (!venues.length) throw new Error('请至少选择一个场馆。');
    const courts = d.courts.filter(c => c.selected).map(c => c.n);
    return {
      venues, reserve_date: d.mode === 'daily' ? null : d.reserveDate, days_ahead: d.daysIndex,
      reserve_time: Array.from({length: end - start}, (_, i) => start + i),
      priority: courts.length ? courts : COURTS.slice(),
      candidates: d.buddies.filter(b => b.checked).map(b => b.index),
      phone: d.phone.trim(), max_pay_amount: d.maxAmount,
      daily_schedule: {login_at: d.loginAt + ':00', release_at: d.releaseAt + ':00', stop_at: d.stopAt + ':00', keep_alive_seconds: KEEP_ALIVE[d.keepAliveIndex]},
      fast: d.fast, remember_session: d.remember
    };
  },

  toggleHall(e) {
    const i = e.currentTarget.dataset.index;
    this.setData({[`halls[${i}].checked`]: !this.data.halls[i].checked});
  },
  onDate(e) { this.reserveDateRolling = false; this.setData({reserveDate: e.detail.value}); },
  onDays(e) {
    const days = Number(e.detail.value);
    this.reserveDateRolling = true;
    this.setData({daysIndex: days, reserveDate: api.beijingDate(this.serverOffset, days)});
  },
  onStart(e) { this.setData({startIndex: Number(e.detail.value)}); this.updateDuration(); },
  onEnd(e) { this.setData({endIndex: Number(e.detail.value)}); this.updateDuration(); },
  updateDuration() {
    this.setData({duration: Math.max(0, END_HOURS[this.data.endIndex] - START_HOURS[this.data.startIndex]) + '小时'});
  },
  toggleCourt(e) {
    const i = e.currentTarget.dataset.index;
    this.setData({[`courts[${i}].selected`]: !this.data.courts[i].selected});
  },
  anyCourt() { this.setData({courts: COURTS.map(n => ({n, selected: false}))}); },
  setMode(e) {
    const mode = e.currentTarget.dataset.mode;
    this.setData({mode, submitText: MODES[mode].submit, modeHint: MODES[mode].hint});
    this.renderPayment();
  },
  onRunDate(e) { this.runDateTouched = true; this.setData({runDate: e.detail.value}); },
  onRunTime(e) { this.runDateTouched = true; this.setData({runTime: e.detail.value}); },
  onTime(e) { this.setData({[e.currentTarget.dataset.field]: e.detail.value}); },
  onDailyEnabled(e) { this.setData({dailyEnabled: e.detail.value}); },
  toggleAdvanced() { this.setData({advanced: !this.data.advanced}); },
  toggleBuddy(e) {
    const i = e.currentTarget.dataset.index;
    this.setData({[`buddies[${i}].checked`]: !this.data.buddies[i].checked});
  },
  onPhone(e) { this.setData({phone: e.detail.value}); },
  onAmount(e) { this.setData({maxAmount: e.detail.value}); },
  onKeepAlive(e) { this.setData({keepAliveIndex: Number(e.detail.value)}); },
  onFast(e) { this.setData({fast: e.detail.value}); },
  onRemember(e) { this.setData({remember: e.detail.value}); },

  async saveSettings() {
    const body = {config: this.readConfig(), daily_enabled: this.data.dailyEnabled};
    await api.request('/api/config', body);
    this.configSnapshot = null;
    wx.showToast({title: '设置已保存', icon: 'success'});
  },

  async onSave() {
    if (this.data.busy || !this.data.ready || !this.data.jobsReady) return;
    this.setData({formError: '', busy: true});
    try { await this.saveSettings(); } catch (error) { this.handleError(error); }
    finally { this.setData({busy: false}); }
  },

  async onSubmit() {
    if (this.data.busy || !this.data.ready || !this.data.jobsReady || (this.data.mode !== 'daily' && this.data.blocked)) return;
    const mode = this.data.mode;
    this.setData({formError: '', busy: true});
    try {
      if (mode === 'daily') await this.saveSettings();
      else {
        if (mode === 'now') {
          const ok = await new Promise(resolve => wx.showModal({
            title: '确认预约', content: '找到空场后会立即提交一笔真实订单，请在约1分钟内扫码付款。',
            confirmText: '立即预约', confirmColor: '#1a6851', success: res => resolve(res.confirm)
          }));
          if (!ok) return;
        }
        const data = await api.request('/api/jobs', {config: this.readConfig(), mode, run_at: mode === 'once' ? this.data.runDate + 'T' + this.data.runTime : null});
        app.globalData.currentId = null;
        wx.showToast({title: mode === 'once' ? '定时任务已创建' : '正在预约', icon: 'none'});
        await this.refreshJobs();
        wx.pageScrollTo({selector: '#payment', duration: 300});
      }
    } catch (error) {
      this.handleError(error);
    } finally {
      this.setData({busy: false});
    }
  },

  async refreshJobs() {
    if (this.refreshing || !api.connection().token) return;
    this.refreshing = true;
    try {
      const data = await api.request('/api/jobs');
      this.serverOffset = api.parseTime(data.server_now) - Date.now();
      this.jobs = data.jobs;
      this.setData({networkError: '', jobsReady: true});
      this.syncDates();
      this.renderPayment();
    } catch (error) {
      if (error.status === 401) { this.stop(); api.goLogin(); }
      else this.setData({networkError: '连接中断，订单状态可能已变化：' + error.message, jobsReady: false});
    } finally {
      this.refreshing = false;
    }
  },

  currentJob() {
    // 付款窗口很短，待付款订单始终优先于历史记录。
    const payment = this.jobs.find(job => job.status === 'awaiting_payment');
    if (payment) return payment;
    const chosen = app.globalData.currentId;
    return (chosen && this.jobs.find(job => job.id === chosen)) || this.jobs.find(job => RUNNING.includes(job.status)) || this.jobs[0];
  },

  renderPayment() {
    const blocked = this.jobs.some(job => RUNNING.includes(job.status) || ['needs_attention', 'qr_needs_attention'].includes(job.status));
    const job = this.currentJob();
    const pinned = !!(job && app.globalData.currentId === job.id);
    if (!job) { this.setData({pay: null, qr: '', live: false, blocked: false, pinned}); return; }
    const status = job.status;
    const pending = status === 'awaiting_payment', paid = status === 'paid';
    const live = !!(pending && job.qr_code && api.parseTime(job.expires_at) > Date.now() + this.serverOffset);
    const alipay = job.qr_kind === 'alipay_payment';
    const pay = {
      id: job.id, status, pending, paid,
      badge: LABELS[status] || status,
      tone: pending || RUNNING.includes(status) ? 'live' : paid ? 'paid' : ['needs_attention', 'qr_needs_attention', 'rejected', 'failed'].includes(status) ? 'bad' : '',
      title: paid ? '预约已确认' : pending ? (alipay ? '支付宝付款码' : '校园付收银台入口码') : LABELS[status] || '预约任务',
      subtitle: paid ? '场馆已确认支付成功，准备上场吧' : pending ? (job.scan_tip || '校园付收银台入口，扫码后按官方页面操作，可能需要登录。') : RUNNING.includes(status) ? '订单生成后，付款码会显示在这里。' : (job.date ? job.date + ' · ' + venueName(job.venue) : ''),
      placeholder: paid ? '预约完成' : status === 'expired' ? '付款码已过期' : '先选场，再扫码',
      message: job.message || LABELS[status] || '等待场馆响应',
      alipayUrl: alipay && /^https:\/\/qr\.alipay\.com\//.test(job.alipay_url || '') ? job.alipay_url : '',
      cashierUrl: /^https:\/\/cashier\.cc-pay\.cn\//.test(job.cashier_url || '') ? job.cashier_url : '',
      hasOrder: !!job.trade_no,
      details: job.trade_no ? [
        ['预约场馆', venueName(job.venue)],
        ['场地时段', `${job.court || ''} · ${(job.slots || []).map(s => s[1] + '–' + s[2]).join(' / ')}`],
        ['预约日期', job.date || ''],
        ['订单编号', job.trade_no],
        ['应付金额', job.amount ? '¥ ' + Number(job.amount).toFixed(2) : '以官方订单为准']
      ].map(([k, v]) => ({k, v})) : [],
      steps: [
        {text: '学校账号已登录', state: 'done'},
        {text: job.trade_no ? '预约订单已生成' : LABELS[status] || '等待预约场次', state: job.trade_no ? 'done' : RUNNING.includes(status) ? 'current' : ''},
        {text: pending ? '请扫码完成付款' : paid ? '支付已确认' : '获取付款二维码', state: paid ? 'done' : pending ? 'current' : ''},
        {text: paid ? '预约有效，准备上场' : '核对预约结果', state: paid ? 'done' : ''}
      ].map((step, k) => Object.assign({k}, step)),
      cancellable: RUNNING.includes(status) && !['submitting', 'locked', 'awaiting_payment'].includes(status),
      resolvable: status === 'needs_attention' && !job.trade_no
    };
    const update = {pay, live, blocked, pinned};
    // 二维码是较大的 base64，只在变化时传给视图层。
    const qr = live ? job.qr_code : '';
    if (qr !== this.data.qr) update.qr = qr;
    this.setData(update);
    if (live && this.lastQrId !== job.id) {
      this.lastQrId = job.id;
      wx.vibrateShort({type: 'heavy'});
      wx.pageScrollTo({selector: '#payment', duration: 300});
    }
    if (paid && this.lastPaid !== job.id) {
      this.lastPaid = job.id;
      wx.showToast({title: '付款已确认，预约成功', icon: 'success'});
    }
    this.updateCountdown();
  },

  updateCountdown() {
    const job = this.currentJob();
    if (!job || job.status !== 'awaiting_payment') return;
    const remaining = Math.max(0, Math.ceil((api.parseTime(job.expires_at) - Date.now() - this.serverOffset) / 1000));
    const update = {
      countdown: String(Math.floor(remaining / 60)).padStart(2, '0') + ':' + String(remaining % 60).padStart(2, '0'),
      progress: Math.min(100, remaining / 60 * 100)
    };
    if (!remaining && this.data.live) Object.assign(update, {live: false, qr: '', 'pay.placeholder': '付款窗口已结束'});
    this.setData(update);
  },

  copyLink(e) {
    const kind = e.currentTarget.dataset.kind;
    const url = kind === 'alipay' ? this.data.pay.alipayUrl : this.data.pay.cashierUrl;
    wx.setClipboardData({
      data: url,
      success: () => wx.showModal({
        title: '链接已复制',
        content: kind === 'alipay' ? '粘贴到手机浏览器打开，会跳转到支付宝确认付款；也可以保存二维码后在支付宝“扫一扫”里选相册。' : '粘贴到手机浏览器打开官方收银台，按页面提示选择支付方式。',
        showCancel: false, confirmColor: '#1a6851'
      })
    });
  },

  saveQr() {
    const match = /^data:image\/(png|jpeg);base64,(.+)$/.exec(this.data.qr || '');
    if (!match) return;
    const path = `${wx.env.USER_DATA_PATH}/cgyy-payment.${match[1] === 'png' ? 'png' : 'jpg'}`;
    wx.getFileSystemManager().writeFile({
      filePath: path, data: match[2], encoding: 'base64',
      success: () => wx.saveImageToPhotosAlbum({
        filePath: path,
        success: () => wx.showToast({title: '已保存到相册', icon: 'success'}),
        fail: err => {
          if (/auth|deny/.test(err.errMsg)) wx.showModal({title: '需要相册权限', content: '请在设置中允许保存到相册。', success: res => res.confirm && wx.openSetting()});
        }
      }),
      fail: () => wx.showToast({title: '二维码保存失败', icon: 'none'})
    });
  },

  async recheck() {
    wx.showLoading({title: '正在核对订单', mask: true});
    try {
      await api.request('/api/recheck', {id: this.currentJob().id});
      await this.refreshJobs();
      wx.hideLoading();
      wx.showToast({title: '已向场馆核对', icon: 'success'});
    } catch (error) {
      wx.hideLoading();
      wx.showToast({title: error.message, icon: 'none'});
    }
  },

  async cancelJob() {
    try {
      await api.request('/api/cancel', {id: this.currentJob().id});
      wx.showToast({title: '已请求停止任务', icon: 'none'});
      await this.refreshJobs();
    } catch (error) {
      wx.showToast({title: error.message, icon: 'none'});
    }
  },

  followLatest() {
    app.globalData.currentId = null;
    this.renderPayment();
  },

  resolveJob() {
    const job = this.currentJob();
    if (!job || !this.data.pay.resolvable) return;
    wx.showModal({
      title: '确认已在官网核对',
      content: '请先登录场馆官网个人中心，确认本次没有有效或待付款订单。确认后将结束这条异常任务并允许重新预约，不会取消任何官方订单。',
      confirmText: '已核对无订单', confirmColor: '#1a6851',
      success: async res => {
        if (!res.confirm) return;
        try {
          await api.request('/api/resolve', {id: job.id, confirmed_no_order: true});
          await this.refreshJobs();
          wx.showToast({title: '异常任务已结束', icon: 'success'});
        } catch (error) { this.handleError(error); }
      }
    });
  },

  logout() {
    wx.showModal({
      title: '切换学校账号', content: '切换账号前须处理运行中及待核对任务。登录其他账号会关闭每日自动预约，配置和记录按账号保存，需重新开启每日预约。', confirmColor: '#1a6851',
      success: async res => {
        if (!res.confirm) return;
        try { await api.request('/api/logout', {}); } catch (error) { /* 令牌失效时直接回到登录页 */ }
        api.saveConnection({token: ''});
        app.globalData.currentId = null;
        this.stop();
        api.goLogin();
      }
    });
  }
});
