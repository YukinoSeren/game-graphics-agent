import { h, panel, badge, alertBox, definitionList, emptyState, formatNumber, keyValueTable, drawer } from '../components.js';
import { buildEditPayload } from '../forms.js';
import { displayValue, presentationText, renderContent } from '../content.js';
import { sourceBody } from './knowledge.js';

const ACTIONS = {
  keep: { label: '保持当前方案', tone: 'ok' },
  measure: { label: '先补测', tone: 'warn' },
  research: { label: '补查关键信息', tone: 'warn' },
  adjust: { label: '建议一项调整', tone: 'info' },
  stop: { label: '停止重复尝试', tone: 'warn' },
  insufficient_evidence: { label: '证据不足', tone: 'warn' },
};

export async function render(root, ctx) {
  const workspace = ctx.workspace;
  const applied = workspace.current_applied_id ? await ctx.entity(workspace.current_applied_id, 'AppliedConfiguration').catch(() => null) : null;
  const summaries = await ctx.entities('Summary').catch(() => []);
  const matching = summaries.filter((item) => item.applied_id === applied?.id);
  const selectedId = ctx.recall('lastSummaryId');
  const summary = matching.find((item) => item.id === selectedId) || matching.at(-1) || null;
  const analyses = await ctx.entities('FeedbackAnalysis').catch(() => []);
  const analysis = analyses.at(-1) || null;
  const task = analysis?.task_id ? await ctx.api.task(analysis.task_id, { scenario: ctx.scenarioId, epoch: workspace.workspace_epoch }).catch(() => null) : null;
  const orphaned = analysis && !task && analysis.status !== 'completed';
  const pending = analysis && task && ['queued', 'running'].includes(analysis.status);
  const action = analysis?.status === 'completed' ? (task?.result || analysis.result || analysis) : null;
  const changeSetId = action?.action === 'adjust' ? action.change_set_id || analysis.change_set_id : null;
  const comparisonId = ctx.recall('comparisonId');
  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('p', { class: 'eyebrow', text: '反馈优化' }),
        h('h2', { text: '这次游戏体验怎么样？' }),
        h('p', { text: '告诉我使用感受，我们一起决定是否需要调整。' }),
      ),
      applied ? badge('已确认设置', 'info') : badge('尚无采用设置', 'warn'),
    ),
  );

  if (pending) {
    ctx.trackTask(analysis.task_id);
    root.append(panel('正在重新分析你的体验反馈',
      h('div', { role: 'status', 'aria-live': 'polite' },
        h('p', { text: '正在结合当前设置、游玩记录与资料依据重新权衡，预计需要 30 秒。' }),
        h('p', { class: 'research-stages', text: '理解反馈 → 对照记录 → 权衡调整' }),
        task?.phases?.[task.phase_index]?.label ? h('p', { class: 'muted', text: task.phases[task.phase_index].label }) : null,
      ), h('button', { type: 'button', class: 'primary', text: '正在分析…', disabled: true }),
    ));
    return;
  }
  if (orphaned) root.append(alertBox('分析记录已恢复，请重新提交反馈', 'warn'));
  else if (analysis && ['failed', 'interrupted', 'cancelled', 'partial'].includes(analysis.status)) {
    root.append(panel('重新分析未完成，请重试', h('p', { text: '输入与分析记录已保留，继续分析会沿用原受理时间。' }), h('button', { type: 'button', class: 'primary', text: '继续分析', onClick: async () => {
      try { const reply = await ctx.send('task.resume', { task_id: analysis.task_id }); ctx.trackTask(reply.task_id || analysis.task_id); await ctx.reload(); } catch (error) { ctx.toast(error.message, 'error'); }
    } })));
  } else if (analysis?.status === 'superseded') root.append(alertBox('配置或分析输入已变化，请重新分析。', 'warn'));

  if (!applied || !summary) {
    root.append(panel('本次体验反馈', emptyState(!applied ? '先确认已在游戏中采用设置，再读取对应的游玩日志。' : '请先读取当前已确认设置对应的一次游玩日志。', h('button', { type: 'button', class: 'primary', text: !applied ? '前往设置详情' : '读取游玩日志', onClick: () => ctx.navigate(!applied ? 'recommendation' : 'performance') }))));
    return;
  }
  const rawText = h('textarea', { dataset: { draftKey: 'feedbackDraft' }, rows: 4, maxLength: 2000, value: ctx.recall('feedbackDraft') ?? (orphaned ? analysis.feedback_snapshot?.raw_text || '' : ''), placeholder: '例如：爆炸时画面卡顿', 'aria-label': '反馈原话' });
  rawText.addEventListener('input', () => ctx.remember('feedbackDraft', rawText.value));
  const satisfied = h('select', { 'aria-label': '是否满意' }, h('option', { value: 'unspecified', text: '未指定' }), h('option', { value: 'yes', text: '满意，没有新问题' }), h('option', { value: 'no', text: '有希望改善的地方' }));
  const severity = h('select', { 'aria-label': '严重程度' }, h('option', { value: 'occasional', text: '偶尔' }), h('option', { value: 'frequent', text: '经常' }), h('option', { value: 'constant', text: '持续' }));
  const preferred = new Set(['texture_quality', 'texture_filtering', 'render_resolution', 'texture_resolution', 'anisotropic_filtering', 'resolution']);
  const protectedIds = [...new Set([...(ctx.catalog.rules.protected_options || []), ...ctx.catalog.settings.filter((item) => preferred.has(item.id) || /纹理|渲染分辨率/.test(item.display_name)).map((item) => item.id)])];
  const protectedBoxes = protectedIds.map((id) => { const setting = ctx.catalog.settings.find((item) => item.id === id); const input = h('input', { type: 'checkbox', value: id, checked: preferred.has(id) || /纹理|渲染分辨率/.test(setting?.display_name || '') }); return { id, input, node: h('label', { class: 'inline' }, input, setting?.display_name || id) }; });
  const errorHost = h('div', { role: 'status' });
  const submit = h('button', { type: 'submit', class: 'primary', text: '提交反馈并获取建议' });
  const form = h('form', {},
    h('div', { class: 'field' }, h('label', { text: '这次有哪些具体感受？' }), rawText),
    h('details', { class: 'advanced-options' }, h('summary', { text: '更多反馈选项' }),
      h('div', { class: 'form-grid' }, h('div', { class: 'field' }, h('label', { text: '是否满意' }), satisfied), h('div', { class: 'field' }, h('label', { text: '严重程度' }), severity)),
      h('h4', { text: '优先保留的画质项' }), h('div', { class: 'inline' }, protectedBoxes.map((item) => item.node))),
    h('div', { class: 'panel-actions' }, submit), errorHost,
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); if (submit.disabled) return;
    if (!rawText.value.trim() && satisfied.value !== 'yes') { errorHost.replaceChildren(alertBox('请写下一句具体感受。', 'warn')); rawText.focus(); return; }
    submit.disabled = true; submit.textContent = '正在提交…';
    try {
      const reply = await ctx.send('feedback.review', { session_id: summary.session_id, summary_id: summary.id, applied_id: applied.id, raw_text: rawText.value, satisfied: satisfied.value === 'unspecified' ? null : satisfied.value === 'yes', severity: severity.value, protected_setting_ids: protectedBoxes.filter((item) => item.input.checked).map((item) => item.id) }, { quiet: true });
      ctx.remember('feedbackAnalysisId', reply.refs?.find((ref) => ref.kind === 'FeedbackAnalysis')?.id || reply.data?.analysis_id);
      ctx.remember('feedbackTaskId', reply.task_id); ctx.trackTask(reply.task_id);
      await ctx.reload();
    } catch (error) { errorHost.replaceChildren(alertBox(error.message)); submit.disabled = false; submit.textContent = '提交反馈并获取建议'; }
  });
  root.append(panel('本次体验反馈', form));

  if (action) {
    const tone = ACTIONS[action.action] || { label: action.action, tone: '' };
    root.append(
      panel('下一步建议',
        h('div', { class: 'inline' }, badge(tone.label, tone.tone)),
        renderContent(action.reason),
      ),
    );
  }

  if (changeSetId) {
    const changeSet = await ctx.entity(changeSetId, 'ChangeSet').catch(() => null);
    if (changeSet) {
      const alreadyAdjusted = changeSet.base_applied_id !== applied.id;
      const evidenceSources = (await Promise.all((changeSet.evidence_ids || []).map((id) => ctx.entity(id, 'Source').catch(() => null)))).filter(Boolean);
      const changes = Object.entries(changeSet.changes).map(([settingId, change]) => ({
        settingId,
        from: change.from,
        to: change.to,
      }));
      root.append(
        panel('建议调整（请先在游戏内手动修改）',
          keyValueTable(
            [
              { key: 'settingId', label: '设置', render: (row) => ctx.catalog.settings.find((item) => item.id === row.settingId)?.display_name || '画质设置' },
              { key: 'from', label: '原值', render: (row) => displayValue(row.from) },
              { key: 'to', label: '建议值', render: (row) => displayValue(row.to) },
            ],
            changes,
          ),
          definitionList([
            ['假设', presentationText(changeSet.hypothesis)],
            ['复测方法', changeSet.retest],
            ['恢复方式', changeSet.recovery.note],
            ['保护项', (changeSet.protected_setting_ids || []).map((id) => ctx.catalog.settings.find((setting) => setting.id === id)?.display_name || '设置项').join('、') || '无'],
          ]),
          h('div', { class: 'panel-actions' },
            h('button', {
              type: 'button', class: 'ghost', text: '查看调整依据',
              onClick: () => {
                const visualCost = changes.some((item) => item.settingId === 'mw2.particle_quality')
                  ? '部分爆炸、火花和烟雾细节减少，整体材质与清晰度保持。'
                  : '部分阴影细节减少，整体材质与清晰度保持。';
                drawer('调整依据与取舍', h('div', {},
                  renderContent(action.reason),
                  ...evidenceSources.map((source) => h('section', { class: 'evidence-source' },
                    h('h4', { text: `${source.source_key || ''} · ${source.title}` }),
                    h('p', { class: 'source-meta', text: source.channel }),
                    h('p', { text: source.summary }),
                    h('details', {}, h('summary', { text: '展开资料正文' }), sourceBody(source)),
                  )),
                  definitionList([['调整假设', changeSet.hypothesis], ['复看方法', changeSet.retest], ['画面代价', changeSet.tradeoff || visualCost]]),
                  h('p', { text: changeSet.observation_note || '用户描述和所选日志支持先尝试降低密集特效负担。日志没有爆炸事件标记，不能据此定位某次爆炸；请在相似交战场景复看。' }),
                ));
              },
            }),
            h('button', {
              type: 'button', class: 'primary', text: alreadyAdjusted ? '配置已变化，请重新分析' : '记录已按建议调整', disabled: alreadyAdjusted,
              onClick: async () => {
                const current = ctx.workspace.current_applied_id;
                if (current !== changeSet.base_applied_id) { ctx.toast('配置已变化，请重新分析', 'warn'); return; }
                const changesMap = Object.fromEntries(
                  changes.map((item) => [item.settingId, item.to]),
                );
                const reply = await ctx.send('configuration.edit', buildEditPayload({
                  expectedCurrentId: current,
                  changes: changesMap,
                  changeSetId: changeSet.id,
                  confirmation: '我已按建议手动调整',
                }));
                ctx.remember('appliedId', reply.refs.find((ref) => ref.kind === 'AppliedConfiguration')?.id);
                ctx.remember('changeSetId', changeSet.id);
                await ctx.reload();
              },
            }),
            h('button', {
              type: 'button', class: 'ghost', text: '下一步：复测',
              onClick: () => ctx.navigate('performance'),
            }),
          ),
        ),
      );
    }
  }

  if (comparisonId && changeSetId) {
    const comparison = await ctx.entity(comparisonId, 'Comparison').catch(() => null);
    if (comparison) {
      const outcomeSaved = ctx.recall('outcomeId');
      root.append(
      panel('是否满意当前体验',
          h('p', { class: 'muted', text: `${comparison.comparable ? '测试条件相同，可比较' : '测试条件不同，仅供参考'} · 长帧判断阈值 ${comparison.threshold_ms} ms` }),
          comparison.metrics.length
            ? keyValueTable(
              [
                { key: 'key', label: '指标' },
                { key: 'before', label: '前' },
                { key: 'after', label: '后' },
                { key: 'delta', label: '变化' },
              ],
              comparison.metrics.map((row) => ({
                ...row,
                delta: row.delta === null ? '未知' : `${row.delta > 0 ? '+' : ''}${formatNumber(row.delta, 2)}`,
              })),
            )
            : null,
          outcomeSaved
            ? h('p', { class: 'muted', text: '你的体验选择已保存。' })
            : h('div', { class: 'panel-actions' },
              h('button', {
                type: 'button', class: 'primary', text: '接受画面与操作体验',
                onClick: async () => {
                  const reply = await ctx.send('outcome.save', {
                    change_set_id: changeSetId,
                    applied_id: ctx.workspace.current_applied_id,
                    comparison_id: comparison.id,
                    accepted: true,
                    reason: '玩家接受画面与操作体验',
                  });
                  ctx.remember('outcomeId', reply.refs.find((ref) => ref.kind === 'Outcome')?.id);
                  await ctx.reload();
                },
              }),
              h('button', {
                type: 'button', class: 'ghost', text: '不接受并手动恢复',
                onClick: async () => {
                  const changeSet = await ctx.entity(changeSetId, 'ChangeSet').catch(() => null);
                  const reply = await ctx.send('outcome.save', {
                    change_set_id: changeSetId,
                    applied_id: ctx.workspace.current_applied_id,
                    comparison_id: comparison.id,
                    accepted: false,
                    reason: '玩家不接受画面，选择恢复',
                  });
                  ctx.remember('outcomeId', reply.refs.find((ref) => ref.kind === 'Outcome')?.id);
                  if (changeSet) {
                    const current = ctx.workspace.current_applied_id;
                    await ctx.send('configuration.rollback', {
                      expected_current_id: current,
                      target_id: changeSet.base_applied_id,
                      confirmation: '我已手动恢复',
                    });
                  }
                  await ctx.reload();
                },
              }),
            ),
        ),
      );
    }
  }
}
