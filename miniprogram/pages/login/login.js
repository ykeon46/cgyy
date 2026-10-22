const api = require('../../utils/api');

function host(server) {
  return server.replace(/^https?:\/\//, '').replace(/\/+$/, '');
}

Page({
  data: {server: '', key: '', serverHost: '', editingServer: true, showKey: false, username: '', password: '', error: '', checking: false, loggingIn: false, connected: false},

  onLoad() {
    const {server, key} = api.connection();
    this.setData({server, key, serverHost: host(server), editingServer: !(server && key)});
    if (server && key) this.checkServer(true);
  },

  onShow() {
    // reLaunch 到非 tab 页时会出现返回首页按钮，登录前不需要。
    if (wx.hideHomeButton) wx.hideHomeButton();
  },

  onServer(e) { this.setData({server: e.detail.value.trim(), connected: false}); },
  onKey(e) { this.setData({key: e.detail.value.trim(), connected: false}); },
  onUsername(e) { this.setData({username: e.detail.value}); },
  onPassword(e) { this.setData({password: e.detail.value}); },
  toggleKey() { this.setData({showKey: !this.data.showKey}); },
  editServer() { this.setData({editingServer: true}); },

  saveServer() {
    const server = this.data.server.replace(/\/+$/, '');
    if (!/^https?:\/\/[^\s/]+/.test(server)) throw new Error('服务器地址须以 https:// 或 http:// 开头。');
    if (this.data.key.length < 16) throw new Error('访问密钥至少16位。');
    api.saveConnection({server, key: this.data.key, token: ''});
    this.setData({serverHost: host(server)});
  },

  async checkServer(quiet) {
    this.setData({error: '', checking: true});
    try {
      this.saveServer();
      await api.request('/api/session');
      this.setData({connected: true, editingServer: false});
      if (quiet !== true) wx.showToast({title: '服务器连接正常', icon: 'success'});
    } catch (error) {
      this.setData({error: error.message, editingServer: true});
    } finally {
      this.setData({checking: false});
    }
  },

  async login() {
    const username = this.data.username.trim();
    if (!username || !this.data.password) {
      this.setData({error: '请填写学校统一认证账号和密码。'});
      return;
    }
    this.setData({error: '', loggingIn: true});
    try {
      this.saveServer();
      const data = await api.request('/api/login', {username, password: this.data.password});
      api.saveConnection({token: data.token});
      this.setData({password: ''});
      wx.switchTab({url: '/pages/index/index'});
    } catch (error) {
      this.setData({error: error.message});
    } finally {
      this.setData({loggingIn: false});
    }
  }
});
