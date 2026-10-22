// 与 web_app.py 通信：小程序没有 Cookie/Origin，使用访问密钥 + 登录后的 Bearer 令牌。
const STORE = 'cgyy_connection';

function connection() {
  return wx.getStorageSync(STORE) || {server: '', key: '', token: ''};
}

function saveConnection(value) {
  wx.setStorageSync(STORE, Object.assign(connection(), value));
}

function request(path, body) {
  const {server, key, token} = connection();
  if (!server || !key) return Promise.reject(Object.assign(new Error('请先填写服务器地址和访问密钥。'), {status: 0}));
  const header = {'X-CGYY-Client': 'miniprogram', 'X-CGYY-Key': key};
  if (token) header.Authorization = 'Bearer ' + token;
  return new Promise((resolve, reject) => {
    wx.request({
      url: server.replace(/\/+$/, '') + path,
      method: body === undefined ? 'GET' : 'POST',
      data: body === undefined ? undefined : JSON.stringify(body),
      header: body === undefined ? header : Object.assign({'Content-Type': 'application/json'}, header),
      timeout: path === '/api/login' || path === '/api/recheck' ? 120000 : 15000,
      success(res) {
        const data = typeof res.data === 'object' && res.data ? res.data : {};
        if (res.statusCode === 401) {
          saveConnection({token: ''});
          reject(Object.assign(new Error('请重新登录学校账号。'), {status: 401}));
        } else if (res.statusCode >= 400) {
          reject(Object.assign(new Error(data.error || '请求未完成，请稍后重试。'), {status: res.statusCode}));
        } else resolve(data);
      },
      fail() {
        reject(Object.assign(new Error('无法连接预约服务器，请检查地址和网络。'), {status: 0}));
      }
    });
  });
}

// Python isoformat 带微秒，统一截到毫秒，避免部分 iOS 内核解析失败。
function parseTime(value) {
  return value ? Date.parse(String(value).replace(/(\.\d{3})\d+/, '$1')) : NaN;
}

// 北京时间日期，offset 为天数。
function beijingDate(serverOffset, offset) {
  const d = new Date(Date.now() + serverOffset + 8 * 3600 * 1000 + (offset || 0) * 86400000);
  return d.toISOString().slice(0, 10);
}

function goLogin() {
  wx.reLaunch({url: '/pages/login/login'});
}

module.exports = {connection, saveConnection, request, parseTime, beijingDate, goLogin};
