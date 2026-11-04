import { h, alertBox } from './components.js';

const LABELS = { 'last-play': ['前一次', '最近一次记录'], 'previous-play': ['前两次', '倒数第二次记录'], 'short-play': ['前三次', '倒数第三次记录'] };
const durationLabel = (seconds) => seconds >= 60 ? `${(seconds / 60).toFixed(1)} 分钟` : `${seconds.toFixed(1)} 秒`;

export async function recentLogsPanel(ctx, applied, run) {
  const logs = ctx.api.recentLogs ? await ctx.api.recentLogs({ scenario: ctx.scenarioId, epoch: ctx.workspace.workspace_epoch }) : [];
  let selected = logs.find((log) => log.id === ctx.recall('selectedRecentLog')) || logs[0];
  const status = h('div', { class: 'recent-log-status', 'aria-live': 'polite' });
  const summary = h('p', { class: 'recent-log-summary' });
  const updateSummary = () => {
    if (!selected) { summary.textContent = '暂无游玩记录。'; return; }
    const date = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(selected.ended_at));
    summary.textContent = `${LABELS[selected.id]?.[1] || selected.label} · ${selected.scene} · ${date} · ${durationLabel(selected.duration_s)}`;
  };
  const choices = h('fieldset', { class: 'recent-log-radios' }, h('legend', { text: '选择要回看的那一次记录' }));
  for (const log of logs) {
    const label = LABELS[log.id] || [log.label, log.label];
    const input = h('input', { type: 'radio', name: 'recent-play-log', id: `recent-${log.id}`, value: log.id, checked: selected?.id === log.id, 'aria-label': `${label[0]}，${label[1]}` });
    input.addEventListener('change', () => { if (!input.checked) return; selected = log; ctx.remember('selectedRecentLog', log.id); updateSummary(); });
    choices.append(h('label', { class: 'recent-radio-label', htmlFor: `recent-${log.id}` }, input, h('span', { text: label[0] })));
  }
  updateSummary();
  const read = h('button', { type: 'button', class: 'primary read-log-button', text: '读取游玩日志', disabled: !selected || !applied || Boolean(run.taskId), onClick: async () => {
    read.disabled = true; read.textContent = '正在读取与分析…';
    status.replaceChildren(h('span', { class: 'muted', text: '正在整理帧时间记录，完成后直接显示结果。' }));
    try { await ctx.send('log.capture_recent', { log_id: selected.id, expected_applied_id: applied.id }, { quiet: true }); ctx.toast('游玩日志已读取，分析结果已准备好。', 'ok'); await ctx.reload(); }
    catch (error) { status.replaceChildren(alertBox(error.message)); }
    finally { read.disabled = !applied || Boolean(run.taskId); read.textContent = '读取游玩日志'; }
  } });
  return h('section', { class: 'recent-logs-panel compact-logs' }, h('h3', { text: '回看游玩记录' }), choices, summary,
    h('div', { class: 'recent-log-actions' }, h('p', { class: 'muted', text: applied ? '按当前已确认设置关联，保留原始记录。' : '请先在设置详情中确认这份日志对应的设置。' }), read), status);
}
