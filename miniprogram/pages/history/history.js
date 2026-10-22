const api = require('../../utils/api');
const {LABELS, RUNNING, venueName} = require('../../utils/constants');

Page({
  data: {jobs: [], error: ''},

  onShow() {
    if (!api.connection().token) return api.goLogin();
    this.load();
  },

  onPullDownRefresh() {
    this.load().then(() => wx.stopPullDownRefresh());
  },

  async load() {
    try {
      const data = await api.request('/api/jobs');
      this.setData({error: '', jobs: data.jobs.map(job => ({
        id: job.id,
        title: venueName(job.venue) || '学院路 · 羽毛球',
        daily: job.source === 'daily',
        subtitle: [job.date || (job.updated_at || '').slice(0, 10), job.court, (job.slots || []).map(s => s[1] + '–' + s[2]).join(' / ')].filter(Boolean).join(' · '),
        tradeNo: job.trade_no || '',
        label: LABELS[job.status] || job.status,
        tone: job.status === 'paid' ? 'good' : RUNNING.includes(job.status) ? 'live' : ['needs_attention', 'qr_needs_attention', 'rejected', 'failed'].includes(job.status) ? 'bad' : ''
      }))});
    } catch (error) {
      if (error.status === 401) return api.goLogin();
      this.setData({error: error.message});
    }
  },

  open(e) {
    getApp().globalData.currentId = e.currentTarget.dataset.id;
    wx.switchTab({url: '/pages/index/index'});
  }
});
