import {
  h, panel, badge, alertBox, emptyState, formatNumber, formatMs, keyValueTable, stat,
} from '../components.js';
import { renderFrameTimeChart } from '../charts.js';
import { displayValue, presentationText } from '../content.js';
import { recentLogsPanel } from '../recent-logs.js';

const LABELS = [
  { value: 'recommended', label: '当前设置' },
  { value: 'adjusted', label: '调整后设置' },
  { value: 'short', label: '补充测试' },
  { value: 'no_gain', label: '再次测试' },
];

function metricGrid(summary) {
  const metrics = summary.metrics || {};
  return h('div', { class: 'metric-grid' },
    stat('平均呈现帧率', metrics.average_present_rate === null ? '未知' : `${formatNumber(metrics.average_present_rate, 1)} fps`, '1000 × 帧数 / 总时长'),
    stat('P95 帧时间', formatMs(metrics.interval_p95_ms)),
    stat('P99 帧时间', formatMs(metrics.interval_p99_ms)),
    stat('最慢 1%', metrics.slowest_1pct_fps === null ? '未知' : `${formatNumber(metrics.slowest_1pct_fps, 1)} fps`, presentationText(metrics.slowest_1pct_reason || '')),
    stat('有效间隔', String(metrics.interval_count ?? 0)),
    stat('质量', displayValue(metrics.quality, '未知'), presentationText(metrics.quality_reason || '')),
  );
}

function summaryView(summary, artifact, ctx) {
  const node = h('div', { class: 'stack' });
  node.dataset.summaryId = summary.id;
  const recipeLabel = ({ baseline: '初始设置', recommended: '当前设置', adjusted: '调整后设置', short: '补充测试', no_gain: '再次测试' })[summary.label] || '测试记录';
  const summaryLabel = artifact?.source === 'recent_log' ? (summary.applied_id === ctx.workspace.current_applied_id ? '当前确认设置' : '此前确认设置') : recipeLabel;
  node.append(
    h('div', { class: 'inline' },
      badge(summaryLabel, 'info'),
      badge(summary.quality, summary.quality === 'usable' ? 'ok' : 'warn'),
    ),
    metricGrid(summary),
  );
  const chartHolder = h('div', {});
  renderFrameTimeChart(chartHolder, summary.chart, { label: `帧时间曲线（${summaryLabel}）` });
  node.append(chartHolder);
  const long = summary.long_frames;
  node.append(
    h('p', { class: 'muted', text: long
      ? `共 ${long.cluster_count} 个长帧簇，${long.interval_count} 个长帧，占有效时长 ${(long.long_frame_time_ratio * 100).toFixed(1)}%，阈值 ${long.threshold_ms} ms。`
      : '没有共同阈值下的长帧统计。' }),
  );
  node.append(
    h('div', { class: 'inline' },
      artifact ? h('button', {
        type: 'button',
        class: 'ghost',
        text: '下载数据文件',
        onClick: async () => {
          const url = await ctx.api.fileUrl(artifact.file_id, {
            scenario: ctx.scenarioId,
            epoch: ctx.workspace.workspace_epoch,
          });
          const anchor = document.createElement('a');
          anchor.href = url;
          anchor.download = '';
          document.body.append(anchor);
          anchor.click();
          anchor.remove();
        },
      }) : null,
    ),
  );
  return node;
}

const TERMINAL = ['completed', 'partial', 'failed', 'cancelled', 'interrupted'];
const RESUMABLE = ['interrupted', 'failed', 'partial'];

function artifactSourceLabel(source) {
  return ({ capture: '采集记录', upload: '导入记录', recent_log: '游玩日志' })[source] || '测试记录';
}

async function loadTask(ctx) {
  const run = ctx.recall('performanceRun') || {};
  if (!run.taskId) return run;
  const task = await ctx.api.task(run.taskId, {
    scenario: ctx.scenarioId,
    epoch: ctx.workspace.workspace_epoch,
  }).catch(() => null);
  if (!task) {
    // The task no longer belongs to this epoch.
    run.taskId = null;
    ctx.remember('performanceRun', run);
    return run;
  }
  if (task.result?.artifact_id) run.artifactId = task.result.artifact_id;
  if (TERMINAL.includes(task.status)) {
    // A terminal task cannot be stopped; keep it only as a resume target.
    run.terminalTaskId = task.id;
    run.terminalTaskStatus = task.status;
    const phases = task.phases || [];
    run.terminalRemaining = Math.max(0, phases.length - (task.phase_index || 0));
    run.taskId = null;
    run.taskStatus = task.status;
  } else {
    run.taskStatus = task.status;
    run.terminalTaskId = null;
    run.terminalTaskStatus = null;
    run.terminalRemaining = 0;
  }
  ctx.remember('performanceRun', run);
  return run;
}

async function refreshRevision(ctx, run) {
  if (!run.segmentId) return run;
  const segment = await ctx.entity(run.segmentId, 'Segment').catch(() => null);
  if (!segment) return run;
  run.segmentRevision = segment.revision;
  run.segmentStatus = segment.status;
  const binding = await ctx.entity(segment.binding_id, 'Binding').catch(() => null);
  if (binding) {
    run.bindingId = binding.id;
    run.bindingRevision = binding.revision;
  }
  ctx.remember('performanceRun', run);
  return run;
}

export async function render(root, ctx) {
  const workspace = ctx.workspace;
  let run = await loadTask(ctx);
  run = await refreshRevision(ctx, run);
  const appliedId = workspace.current_applied_id;
  const applied = appliedId
    ? await ctx.entity(appliedId, 'AppliedConfiguration').catch(() => null)
    : null;
  const planId = ctx.recall('planId');
  const availableSummaries = await ctx.entities('Summary').catch(() => []);
  const summaryIds = availableSummaries.length ? availableSummaries.map((item) => item.id) : ctx.recall('summaryIds') || [];
  const protocolId = ctx.recall('protocolId');
  const comparisonId = ctx.recall('comparisonId');

  let segmentArtifacts = [];
  if (run.segmentId && run.source !== 'recent_log') {
    const all = await ctx.api
      .entities('LogArtifact', {
        scenario: ctx.scenarioId,
        epoch: workspace.workspace_epoch,
      })
      .catch(() => []);
    segmentArtifacts = all
      .filter((artifact) => artifact.segment_id === run.segmentId)
      .sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const known = segmentArtifacts.some((artifact) => artifact.id === run.artifactId);
    if (!known && segmentArtifacts.length) {
      // Make the analysis target explicit instead of silently reusing whichever
      // artifact the previous step happened to leave behind.
      run.artifactId = segmentArtifacts[segmentArtifacts.length - 1].id;
      ctx.remember('performanceRun', run);
    }
  }
  const selectedArtifact =
    segmentArtifacts.find((artifact) => artifact.id === run.artifactId) || null;

  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('p', { class: 'eyebrow', text: '测试与结果' }),
        h('h2', { text: '看看上次玩得怎么样' }),
        h('p', { text: '读取游玩日志，了解画面流畅度和需要留意的卡顿。' }),
      ),
      badge(applied ? '已确认设置' : '尚未采用设置', applied ? 'ok' : 'warn'),
    ),
  );

  root.append(await recentLogsPanel(ctx, applied, run));

  if (!applied) {
    root.append(panel('开始测试', emptyState('先保存并采用一个配置版本，才能创建绑定区段。',
      h('button', { type: 'button', class: 'primary', text: '去画质方案', onClick: () => ctx.navigate('recommendation') }),
    )));
  }

  const sceneInput = h('input', { type: 'text', value: presentationText(ctx.catalog.context.scene_tag), 'aria-label': '测试场景' });
  const durationInput = h('input', { type: 'number', min: '1', value: '60', 'aria-label': '计划时长' });
  const repeatsInput = h('input', { type: 'number', min: '1', value: '1', 'aria-label': '重复次数' });
  const labelSelect = h('select', { 'aria-label': '测试类型' },
    ...LABELS.map((item) => h('option', { value: item.value, text: item.label })),
  );

  root.append(
    h('details', { class: 'advanced-options', open: Boolean(run.taskId) },
      h('summary', { text: '新建一次测试' }),
      h('div', { class: 'grid two' },
      panel('测试计划',
        h('p', { class: 'muted', text: planId ? '测试计划已保存' : '选择测试场景与时长。' }),
        h('div', { class: 'form-grid' },
          h('div', { class: 'field' }, h('label', { text: '场景' }), sceneInput),
          h('div', { class: 'field' }, h('label', { text: '时长（秒）' }), durationInput),
          h('div', { class: 'field' }, h('label', { text: '重复次数' }), repeatsInput),
        ),
        h('div', { class: 'panel-actions' },
          h('button', {
            type: 'button', class: 'ghost', text: '保存计划',
            onClick: async () => {
              const reply = await ctx.send('plan.save', {
                scene: sceneInput.value,
                duration_s: Number(durationInput.value) || null,
                repeats: Number(repeatsInput.value) || null,
              });
              ctx.remember('planId', reply.refs.find((ref) => ref.kind === 'TestPlan')?.id);
              await ctx.reload();
            },
          }),
        ),
      ),
      panel('开始测试',
        h('p', { class: 'muted', text: applied ? '开始后会创建新的测试记录。' : '需要先确认采用设置。' }),
        h('div', { class: 'field' }, h('label', { text: '测试条件' }), labelSelect),
        h('div', { class: 'panel-actions' },
          h('button', {
            type: 'button', class: 'primary', text: '开始测试', disabled: !applied || Boolean(run.taskId),
            onClick: async () => {
              const sessionReply = await ctx.send('session.create', {
                context_id: workspace.context_id,
                applied_id: appliedId,
                plan_id: ctx.recall('planId'),
                run_conditions: { scene: sceneInput.value, note: '测试记录' },
              });
              const sessionId = sessionReply.refs.find((ref) => ref.kind === 'Session')?.id;
              const segment = sessionReply.data;
              run = {
                sessionId,
                segmentId: segment.segment_id,
                segmentRevision: segment.segment_revision,
                bindingId: segment.binding_id,
                bindingRevision: segment.binding_revision,
                segmentStatus: 'open',
              };
              const capture = await ctx.send('capture.start', {
                session_id: sessionId,
                segment_id: segment.segment_id,
                expected_segment_revision: segment.segment_revision,
                expected_binding_revision: segment.binding_revision,
                label: labelSelect.value,
              });
              run.taskId = capture.task_id;
              ctx.remember('performanceRun', run);
              ctx.trackTask(capture.task_id);
              await ctx.reload();
            },
          }),
          run.taskId ? h('button', {
            type: 'button', class: 'ghost', text: '结束测试',
            onClick: async () => {
              const reply = await ctx.send('capture.stop', { task_id: run.taskId });
              run.artifactId = reply.data.artifact_id;
              run.taskId = null;
              ctx.remember('performanceRun', run);
              await ctx.reload();
            },
          }) : null,
          run.taskId && workspace.mode === 'step' ? h('button', {
            type: 'button', class: 'ghost', text: '推进下一阶段（分步）',
            onClick: async () => {
              await ctx.send('task.advance', { task_id: run.taskId });
              await ctx.reload();
            },
          }) : null,
          run.terminalTaskId && RESUMABLE.includes(run.terminalTaskStatus)
            && !run.artifactId && run.terminalRemaining > 0 ? h('button', {
            type: 'button', class: 'primary', text: '继续剩余阶段',
            onClick: async () => {
              const reply = await ctx.send('task.resume', { task_id: run.terminalTaskId });
              run.taskId = reply.task_id;
              run.terminalTaskId = null;
              run.terminalTaskStatus = null;
              run.terminalRemaining = 0;
              ctx.remember('performanceRun', run);
              ctx.trackTask(reply.task_id);
              await ctx.reload();
            },
          }) : null,
          run.terminalTaskId && !run.artifactId ? h('button', {
            type: 'button', class: 'ghost', text: '放弃本次（新建会话）',
            onClick: async () => {
              ctx.remember('performanceRun', {});
              ctx.reload();
            },
          }) : null,
        ),
      ),
      ),
    ),
  );

  if (run.segmentId && run.source !== 'recent_log') {
    const artifactSelect = h('select', { 'aria-label': '本次分析原件' },
      ...segmentArtifacts.map((artifact) => h('option', {
        value: artifact.id,
        text: `${artifact.label} · ${artifactSourceLabel(artifact.source)}`,
        selected: artifact.id === run.artifactId,
      })),
    );
    artifactSelect.addEventListener('change', async () => {
      run.artifactId = artifactSelect.value;
      ctx.remember('performanceRun', run);
      await ctx.reload();
    });
    const target = selectedArtifact
      ? h('p', { class: 'muted', text: '测试数据已准备好，可以查看分析结果。' })
      : h('p', { class: 'muted', text: '尚未选择可分析原件。' });
    root.append(
      panel('当前测试',
        h('div', { class: 'inline' },
          badge(run.segmentStatus === 'closed' ? '已结束' : '进行中', run.segmentStatus === 'closed' ? 'ok' : 'warn'),
        ),
        segmentArtifacts.length > 1
          ? h('div', { class: 'field' }, h('label', { text: '本次分析原件（显式选择）' }), artifactSelect)
          : null,
        target,
        h('div', { class: 'panel-actions' },
          run.artifactId && run.segmentStatus === 'closed' ? h('button', {
            type: 'button', class: 'primary', text: '分析所选原件',
            onClick: async () => {
              const reply = await ctx.send('performance.analyze', {
                artifact_id: run.artifactId,
                segment_id: run.segmentId,
                segment_revision: run.segmentRevision,
                binding_revision: run.bindingRevision,
              });
              const summaryId = reply.refs.find((ref) => ref.kind === 'Summary')?.id;
              const summaries = [...(ctx.recall('summaryIds') || [])];
              if (summaryId && !summaries.includes(summaryId)) summaries.push(summaryId);
              ctx.remember('summaryIds', summaries);
              ctx.remember('lastSummaryId', summaryId);
              await ctx.reload();
            },
          }) : h('span', { class: 'muted', text: run.artifactId ? '请先结束当前测试。' : '完成测试后可查看分析结果。' }),
        ),
      ),
    );
  }

  const summaries = [];
  for (const id of summaryIds) {
    const summary = await ctx.entity(id, 'Summary').catch(() => null);
    if (summary) summaries.push(summary);
  }

  if (summaries.length) {
    const latest = summaries.find((item) => item.id === ctx.recall('lastSummaryId')) || summaries[summaries.length - 1];
    const artifact = await ctx.entity(latest.artifact_id, 'LogArtifact').catch(() => null);
    const session = await ctx.entity(latest.session_id, 'Session').catch(() => null);
    const logId = session?.run_conditions?.virtual_log_id;
    const label = { 'last-play': '前一次 · 最近一次记录', 'previous-play': '前两次 · 倒数第二次记录', 'short-play': '前三次 · 倒数第三次记录' }[logId];
    root.append(panel('游玩日志结果', label ? h('p', { class: 'summary-origin', text: `结果来自：${label}` }) : null, summaryView(latest, artifact, ctx)));
    ctx.remember('lastSummaryId', latest.id);
    ctx.remember('summaryIds', summaryIds);
  } else {
    root.append(panel('测试结果', emptyState('读取游玩日志，或完成一次测试后，结果会显示在这里。')));
  }

  root.append(
    panel('比较口径与前后差异',
      h('p', { class: 'muted', text: '开始对比前先固定相同的比较条件，之后可查看调整前后的变化。' }),
      h('div', { class: 'panel-actions' },
        h('button', {
          type: 'button', class: 'ghost', text: '冻结比较口径（50 ms）',
          disabled: summaries.length === 0,
          onClick: async () => {
            const reply = await ctx.send('comparison.freeze', {
              mode: 'fixed',
              threshold_ms: 50.0,
              baseline_summary_ids: [summaries[0].id],
              exploratory: false,
            });
            ctx.remember('protocolId', reply.refs.find((ref) => ref.kind === 'ComparisonProtocol')?.id);
            await ctx.reload();
          },
        }),
        h('button', {
          type: 'button', class: 'primary', text: '比较最早与最新摘要',
          disabled: !protocolId || summaries.length < 2,
          onClick: async () => {
            const reply = await ctx.send('comparison.run', {
              protocol_id: protocolId,
              before_summary_id: summaries[0].id,
              after_summary_id: summaries[summaries.length - 1].id,
            });
            ctx.remember('comparisonId', reply.refs.find((ref) => ref.kind === 'Comparison')?.id);
            await ctx.reload();
          },
        }),
      ),
      protocolId ? h('p', { class: 'muted', text: '前后比较条件已保存。' }) : null,
      comparisonId ? await comparisonView(ctx, comparisonId) : null,
    ),
  );

  root.append(h('div', { class: 'panel-actions' }, h('button', { type: 'button', class: 'primary', text: '前往体验反馈', onClick: () => ctx.navigate('feedback') })));
  root.append(h('details', { class: 'advanced-options' },
    h('summary', { text: '高级数据选项 · 导入文件' }), uploadPanel(ctx, run),
  ));
}

async function comparisonView(ctx, comparisonId) {
  const comparison = await ctx.entity(comparisonId, 'Comparison').catch(() => null);
  if (!comparison) return h('p', { class: 'empty', text: '比较结果不可用。' });
  const rows = comparison.metrics.map((row) => ({
    ...row,
    deltaText: row.delta === null ? '未知' : `${row.delta > 0 ? '+' : ''}${formatNumber(row.delta, 2)} ${row.unit}`,
  }));
  return h('div', { class: 'stack' },
    h('div', { class: 'inline' },
      badge(comparison.comparable ? '同口径可比较' : '仅探索性差异', comparison.comparable ? 'ok' : 'warn'),
      h('span', { class: 'muted', text: `共同阈值 ${comparison.threshold_ms} ms` }),
      comparison.exploratory ? badge('探索性', 'warn') : null,
    ),
    keyValueTable(
      [
        { key: 'key', label: '指标' },
        { key: 'before', label: '前' },
        { key: 'after', label: '后' },
        { key: 'deltaText', label: '变化' },
      ],
      rows,
    ),
    h('p', { class: 'muted', text: '数据变化供参考，是否接受以实际游戏体验为准。' }),
    ...(comparison.comparability_notes || []).map((note) => alertBox(note, 'warn')),
  );
}

function uploadPanel(ctx, run) {
  const fileInput = h('input', { type: 'file', accept: '.csv,text/csv' });
  const preview = h('div', {});
  return panel('导入性能数据',
    h('p', { class: 'muted', text: '先预览数据，再选择要关联的测试记录。' }),
    h('div', { class: 'field' }, h('label', { text: '选择 CSV' }), fileInput),
    h('div', { class: 'panel-actions' },
      h('button', {
        type: 'button', class: 'ghost', text: '上传并预览',
        onClick: async () => {
          const file = fileInput.files[0];
          if (!file) {
            preview.replaceChildren(alertBox('请先选择 CSV 文件。', 'warn'));
            return;
          }
          try {
            const upload = await ctx.upload(file);
            const fileId = upload.refs.find((ref) => ref.kind === 'File')?.id;
            const reply = await ctx.send('log.inspect', { file_id: fileId });
            const inspection = reply.data.inspection;
            preview.replaceChildren(
              h('p', { text: `识别到 ${inspection.frame_count} 帧，时长 ${inspection.duration_s} 秒，问题 ${inspection.issues.length} 条。` }),
              h('p', { class: 'muted', text: '文件已读取，可选择关联到测试记录。' }),
              run.segmentId && run.segmentStatus === 'closed' ? h('button', {
                type: 'button', class: 'primary', text: '关联到已结束的测试',
                onClick: async () => {
                  const related = await ctx.send('log.associate', {
                    file_id: fileId,
                    segment_id: run.segmentId,
                    segment_revision: run.segmentRevision,
                    binding_revision: run.bindingRevision,
                  });
                  const artifactId = related.refs.find((ref) => ref.kind === 'LogArtifact')?.id;
                  if (artifactId) {
                    // This upload is now the explicit analysis target.
                    run.artifactId = artifactId;
                    run.segmentStatus = 'closed';
                    ctx.remember('performanceRun', run);
                    await refreshRevision(ctx, run);
                  }
                  preview.replaceChildren(
                    h('p', { class: 'muted', text: '已关联到测试记录，可继续查看分析结果。' }),
                  );
                  await ctx.reload();
                },
              }) : h('p', { class: 'warn', text: '需要先完成一次测试才能关联。' }),
            );
          } catch (error) {
            preview.replaceChildren(alertBox(error.message, 'error'));
          }
        },
      }),
    ),
    preview,
  );
}
