import { h, panel, badge, drawer, emptyState, keyValueTable } from '../components.js';
import { presentationText } from '../content.js';

export function sourceBody(source) {
  const body = h('div', { class: 'source-body' });
  const blocks = source.display_blocks?.length ? source.display_blocks : source.content_blocks || [];
  for (const block of blocks) {
    if (block.heading) body.append(h('h4', { text: block.heading }));
    if (block.type === 'table') {
      body.append(keyValueTable(block.columns.map((label) => ({ key: label, label })), block.rows.map((row) => Object.fromEntries(block.columns.map((label, index) => [label, row[index]])))));
    } else {
      for (const paragraph of String(block.text || '').split(/\n\s*\n/)) if (paragraph.trim()) body.append(h('p', { text: paragraph }));
    }
  }
  return body;
}

export async function openSource(ctx, sourceId) {
  const source = await ctx.entity(sourceId, 'Source');
  drawer(source.title, h('div', {}, h('p', { class: 'source-meta', text: `${source.source_key || ''} · ${source.channel || '资料'}` }), sourceBody(source), source.limitations ? h('p', { class: 'muted', text: source.limitations }) : null));
}

export async function render(root, ctx) {
  const taskId = ctx.recall('knowledgeTaskId');
  const task = taskId ? await ctx.api.task(taskId, { scenario: ctx.scenarioId, epoch: ctx.workspace.workspace_epoch }).catch(() => null) : null;
  if (task?.result?.knowledge_id && ctx.recall('knowledgeConsumedTask') !== task.id) {
    ctx.remember('knowledgeConsumedTask', task.id); ctx.remember('knowledgeId', task.result.knowledge_id);
  }
  const cards = await ctx.entities('GameCard').catch(() => []);
  const knowledgeId = ctx.recall('knowledgeId') || cards.at(-1)?.knowledge_id;
  const snapshot = knowledgeId ? await ctx.entity(knowledgeId, 'KnowledgeSnapshot').catch(() => null) : null;
  root.append(h('div', { class: 'page-head' }, h('div', {}, h('p', { class: 'eyebrow', text: '资料与依据' }), h('h2', { text: '游戏资料库' }), h('p', { text: snapshot ? `已整理 ${snapshot.source_ids.length} 份资料，覆盖设备条件、画质取舍与密集交战场景。` : '资料整理完成后，可在这里查看来源与依据。' })), snapshot ? badge('已完成', 'ok') : null));
  if (snapshot) {
    const sources = (await Promise.all(snapshot.source_ids.map((id) => ctx.entity(id, 'Source').catch(() => null)))).filter(Boolean);
    const list = h('div', { class: 'source-library' });
    const filters = h('div', { class: 'source-filters', role: 'group', 'aria-label': '按资料渠道筛选' });
    let active = '';
    const draw = () => {
      list.replaceChildren(...sources.filter((source) => !active || source.channel === active).map((source) => h('article', { class: 'source-card' },
        h('p', { class: 'source-meta', text: `${source.source_key || ''} · ${source.channel || '资料'}` }),
        h('h3', { text: source.title }),
        h('p', { class: 'source-summary', text: source.summary || (source.display_blocks || source.content_blocks || []).find((block) => block.text)?.text || '' }),
        h('p', { class: 'source-settings', text: `关联设置：${(source.setting_ids || []).map((id) => ctx.catalog.settings.find((item) => item.id === id)?.display_name || '整体方案').join('、')}` }),
        h('button', { type: 'button', class: 'text-button', text: '查看资料', onClick: () => openSource(ctx, source.id) }),
      )));
    };
    const channels = ['', ...new Set(sources.map((source) => source.channel).filter(Boolean))];
    const buttons = channels.map((channel) => h('button', { type: 'button', class: 'ghost', 'aria-pressed': channel === active ? 'true' : 'false', text: `${channel || '全部'} ${sources.filter((source) => !channel || source.channel === channel).length}`, onClick: () => {
      active = channel;
      buttons.forEach((button, index) => button.setAttribute('aria-pressed', channels[index] === active ? 'true' : 'false'));
      draw();
    } }));
    filters.append(...buttons); draw(); root.append(filters, list);
    root.append(h('div', { class: 'panel-actions' }, h('button', { type: 'button', class: 'primary', text: '查看设置详情', onClick: () => ctx.navigate('recommendation') })));
  } else root.append(emptyState('请回到助手完成游戏资料与设置方案的整理。', h('button', { type: 'button', class: 'primary', text: '返回助手', onClick: () => ctx.navigate('conversation') })));
  root.append(await advancedTools(ctx, snapshot));
}

async function advancedTools(ctx, snapshot) {
  const tools = h('details', { class: 'advanced-options' }, h('summary', { text: '高级资料工具 · 整理、导入与更新' }));
  const mode = h('select', { 'aria-label': '采集模式' }, h('option', { value: 'discover', text: '整理现有资料' }), h('option', { value: 'seeded', text: '使用指定资料' }));
  const stages = h('input', { type: 'number', min: 1, max: 6, value: 6, 'aria-label': '阶段预算' });
  const sources = h('input', { type: 'number', min: 1, max: 15, value: 15, 'aria-label': '来源预算' });
  const file = h('input', { type: 'file', accept: '.json,application/json', 'aria-label': '选择 JSON 知识包' });
  const operate = (action, payload) => async () => { try { const reply = await ctx.send(action, payload()); const id = reply.refs?.find((ref) => ref.kind === 'KnowledgeSnapshot')?.id; if (id) ctx.remember('knowledgeId', id); if (reply.task_id) { ctx.remember('knowledgeTaskId', reply.task_id); ctx.trackTask(reply.task_id); } await ctx.reload(); } catch (error) { ctx.toast(error.message, 'error'); } };
  tools.append(panel('资料整理', h('div', { class: 'form-grid' }, h('div', { class: 'field' }, h('label', { text: '采集模式' }), mode), h('div', { class: 'field' }, h('label', { text: '最多阶段' }), stages), h('div', { class: 'field' }, h('label', { text: '最多来源' }), sources)), h('button', { type: 'button', class: 'ghost', text: '开始整理资料', onClick: operate('knowledge.collect', () => ({ mode: mode.value, budget: { max_stages: Number(stages.value), max_sources: Number(sources.value) } })) })));
  tools.append(panel('导入资料', file, h('button', { type: 'button', class: 'ghost', text: '上传并导入', onClick: async () => { if (!file.files[0]) { ctx.toast('请先选择知识包文件。', 'warn'); return; } try { const uploaded = await ctx.upload(file.files[0]); await operate('knowledge.import', () => ({ file_id: uploaded.refs.find((ref) => ref.kind === 'File')?.id }))(); } catch (error) { ctx.toast(error.message, 'error'); } } })));
  if (snapshot) {
    const gaps = (await Promise.all((snapshot.gap_ids || []).map((id) => ctx.entity(id, 'KnowledgeGap').catch(() => null)))).filter(Boolean);
    tools.append(panel('资料版本与补充', ...gaps.map((gap) => h('div', { class: 'inline' }, h('span', { text: presentationText(`${gap.label}：${gap.detail}`) }), gap.status !== 'resolved' ? h('button', { type: 'button', class: 'ghost', text: '补充资料', onClick: operate('knowledge.followup', () => ({ knowledge_id: snapshot.id, gap_id: gap.id })) }) : null)), h('button', { type: 'button', class: 'ghost', text: '更新资料', onClick: operate('knowledge.update', () => ({ knowledge_id: snapshot.id })) })));
  }
  const taskId = ctx.recall('knowledgeTaskId');
  const task = taskId ? await ctx.api.task(taskId, { scenario: ctx.scenarioId, epoch: ctx.workspace.workspace_epoch }).catch(() => null) : null;
  if (task) {
    const busy = ['queued', 'running'].includes(task.status);
    if (busy) ctx.trackTask(task.id);
    tools.append(panel('整理进度', h('p', { text: task.phases?.[task.phase_index]?.label || task.status }), busy && ctx.workspace.mode === 'step' ? h('button', { type: 'button', class: 'ghost', text: '继续下一阶段', onClick: operate('task.advance', () => ({ task_id: task.id })) }) : null, busy ? h('button', { type: 'button', class: 'ghost', text: '暂停整理', onClick: operate('task.cancel', () => ({ task_id: task.id })) }) : ['cancelled', 'partial', 'interrupted', 'failed'].includes(task.status) ? h('button', { type: 'button', class: 'ghost', text: '继续整理', onClick: operate('task.resume', () => ({ task_id: task.id })) }) : null));
  }
  return tools;
}
