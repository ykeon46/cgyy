// 与 web/app.js 保持一致。
const HALLS = ['学院路校区-体育馆主馆-羽毛球', '学院路校区-体育馆训练馆(副馆)-羽毛球'];
const COURTS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 17, 18, 19, 20, 21, 22, 23, 24];
const LABELS = {queued: '任务已排队', logging_in: '正在登录', waiting_start: '等待启动', waiting_release: '等待开放', keeping_session: '保持登录', session_valid: '登录有效', ready: '准备就绪', checking: '正在查找场次', submitting: '正在提交预约', locked: '订单已生成', awaiting_payment: '待扫码付款', paid: '预约成功', expired: '付款已超时', cancelled: '订单已取消', failed: '任务未完成', rejected: '下单被拒绝', needs_attention: '需要核对订单', qr_needs_attention: '需要核对付款', stopped: '任务已停止', waiting_availability: '等待空闲场次', no_availability: '未找到合适场次', read_retry: '正在重新读取'};
LABELS.resolved = '已人工核对';
const RUNNING = ['queued', 'logging_in', 'waiting_start', 'waiting_release', 'keeping_session', 'session_valid', 'ready', 'checking', 'submitting', 'locked', 'awaiting_payment', 'waiting_availability', 'read_retry'];

function venueName(venue) {
  return (venue || '').replace('学院路校区-', '').replace('-羽毛球', '');
}

module.exports = {HALLS, COURTS, LABELS, RUNNING, venueName};
