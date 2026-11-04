import {
  h, panel, badge, drawer, definitionList, emptyState,
} from '../components.js';
import { buildAdoptionPayload, buildBaselinePayload, recommendationSettings } from '../forms.js';
import { displayValue, renderContent } from '../content.js';
import { sourceBody } from './knowledge.js';

function confirmTextDialog({ title, body, label, defaultValue, confirmLabel }) {
  return new Promise((resolve) => {
    const input = h('input', { type: 'text', value: defaultValue || '', 'aria-label': label });
    const dialog = h('dialog', { class: 'modal' },
      h('h3', { text: title }),
      h('p', { class: 'modal-body', text: body }),
      h('div', { class: 'field' }, h('label', { text: label }), input),
      h('div', { class: 'modal-actions' },
        h('button', { type: 'button', class: 'ghost', text: '取消', onClick: () => { dialog.close(); dialog.remove(); resolve(null); } }),
        h('button', {
          type: 'button', class: 'primary', text: confirmLabel,
          onClick: () => { const value = input.value.trim(); dialog.close(); dialog.remove(); resolve(value || null); },
        }),
      ),
    );
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
}

async function showEvidence(ctx, item) {
  const evidence = item.evidence_decision || {};
  const body = h('div', { class: 'evidence-body' }, definitionList([['设置', item.display_name], ['建议值', displayValue(item.value)]]));
  body.append(h('h4', { text: '来源与观点' }));
  const views = evidence.source_views || item.source_views || (item.evidence_ids || []).map((id) => ({ source_id: id }));
  for (const view of views) {
    const source = view.source_id ? await ctx.entity(view.source_id, 'Source').catch(() => null) : null;
    body.append(h('section', { class: 'evidence-source' },
      h('h5', { text: `${view.source_key || source?.source_key || ''} · ${view.title || source?.title || '相关资料'}` }),
      h('p', { class: 'source-meta', text: view.channel || source?.channel || '资料' }),
      view.applicability ? h('p', { class: 'muted', text: view.applicability }) : null,
      view.view ? renderContent(view.view) : source?.summary ? h('p', { text: source.summary }) : null,
      source ? h('details', {}, h('summary', { text: '展开资料正文' }), sourceBody(source)) : null,
    ));
  }
  if (evidence.alternatives?.length) {
    body.append(h('h4', { text: '分歧与可选做法' }));
    for (const alternative of evidence.alternatives) body.append(typeof alternative === 'object' ? definitionList([['可选值', displayValue(alternative.value)], ['收益', alternative.benefit], ['代价', alternative.cost], ['未选择的原因', alternative.not_selected_reason]].filter(([, value]) => value)) : renderContent(alternative));
  }
  if (evidence.alternatives_note) body.append(h('h4', { text: '可选做法' }), renderContent(evidence.alternatives_note));
  const sections = [ ['最终决定', evidence.decision || item.reason], ['画面与流畅度取舍', evidence.tradeoff], ['与你的体验目标的关系', evidence.user_goal_fit], ['设备与适用条件', evidence.device_context || (item.dependencies || []).join('；')], ['何时重新评估', evidence.revisit_when]];
  for (const [heading, text] of sections) if (text) body.append(h('h4', { text: heading }), renderContent(typeof text === 'object' ? displayValue(text) : text));
  drawer(`${item.display_name} · 设置依据`, body);
}

function settingsTable(recommendation, currentApplied, ctx) {
  const groups = new Map();
  for (const setting of recommendation.settings) {
    const group = setting.group || '其他设置';
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(setting);
  }
  return h('div', { class: 'settings-catalog' }, ...[...groups].map(([name, settings], index) =>
    h('section', { class: 'settings-group' },
      h('div', { class: 'settings-group-heading' },
        h('span', { class: 'settings-group-index', text: String(index + 1).padStart(2, '0') }),
        h('h4', { text: name }),
        h('span', { class: 'muted', text: `${settings.length} 项设置` }),
      ),
      h('div', { class: 'settings-grid' }, ...settings.map((setting) => {
        const current = currentApplied?.settings?.[setting.setting_id];
        const changed = current !== null && current !== undefined && current !== setting.value;
        return h('article', { class: `setting-item ${changed ? 'has-change' : ''}` },
          h('div', { class: 'setting-item-heading' },
            h('h5', { text: setting.display_name }),
            setting.needs_verification ? badge('待核对', 'warn') : null,
          ),
          h('div', { class: 'setting-values' },
            h('div', {}, h('span', { class: 'setting-value-label', text: '建议设置' }),
              h('strong', { class: 'setting-suggested', text: displayValue(setting.value, '待核对') })),
            h('div', { class: 'setting-current' },
              h('span', { class: 'setting-value-label', text: '当前记录' }),
              h('span', { text: displayValue(current, '尚未记录') }),
            ),
          ),
          h('div', { class: 'setting-item-footer' },
            h('button', { type: 'button', class: 'text-button', text: '查看依据',
              onClick: () => showEvidence(ctx, setting) }),
          ),
        );
      })),
    ),
  ));
}

export async function render(root, ctx) {
  const cards = await ctx.entities('GameCard').catch(() => []);
  const card = cards.at(-1);
  const workspace = ctx.workspace;
  const currentApplied = workspace.current_applied_id
    ? await ctx.entity(workspace.current_applied_id, 'AppliedConfiguration').catch(() => null)
    : null;

  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('p', { class: 'eyebrow', text: '设置详情' }),
        h('h2', { text: '为你的体验整理的设置建议' }),
        h('p', { text: '请先在游戏内完成调整，再确认采用。' }),
      ),
      currentApplied ? badge('已确认采用', 'info') : badge('尚未采用'),
    ),
  );

  let recommendationId = ctx.recall('recommendationId') || card?.recommendation_id;
  let recommendation = null;
  if (recommendationId) {
    recommendation = await ctx.entity(recommendationId, 'Recommendation').catch(() => null);
  }
  if (!recommendation && card?.recommendation_id) {
    recommendation = await ctx.entity(card.recommendation_id, 'Recommendation').catch(() => null);
  }

  if (!recommendation) {
    root.append(panel('设置详情', emptyState('这份历史游戏卡还没有关联设置方案，请返回助手重新整理。', h('button', { type: 'button', class: 'primary', text: '返回助手整理方案', onClick: () => ctx.navigate('conversation') }))));
    return;
  }
  root.append(panel('已有设置方案', definitionList([['主方案', recommendation.label], ['设置项', String(recommendation.settings.length)], ['体验目标', '流畅优先，尽量保留画面丰富度']])));

  const variants = (await Promise.all((card?.variant_ids || []).map((id) => ctx.entity(id, 'Recommendation').catch(() => null)))).filter(Boolean);
  const variantButtons = h('div', { class: 'panel-actions' }, [
    card?.recommendation_id ? { id: card.recommendation_id, label: '平衡方案' } : null, ...variants,
  ].filter(Boolean).map((variant) => h('button', { type: 'button', class: 'ghost', text: `查看：${variant.label || displayValue(variant.template)}`, onClick: async () => { ctx.remember('recommendationId', variant.id); await ctx.reload(); } })));

  root.append(
    panel(`画质设置 · ${recommendation.settings.length} 项`,
      h('details', { class: 'advanced-options' }, h('summary', { text: '其他预设方案' }), variantButtons),
      settingsTable(recommendation, currentApplied, ctx),
    ),
  );

  const adoptActions = h('div', { class: 'panel-actions' });
  if (!currentApplied) {
    adoptActions.append(
      h('button', {
        type: 'button', class: 'ghost', text: '保存当前设置',
        onClick: async () => {
          const confirmation = await confirmTextDialog({
            title: '保存当前设置',
            body: '记录你当前在游戏内使用的设置。',
            label: '确认声明',
            defaultValue: '这是我在调整前使用的配置',
            confirmLabel: '保存',
          });
          if (!confirmation) return;
          const baseline = buildBaselinePayload({
            expectedCurrentId: null,
            settings: ctx.catalog.templates.baseline.values,
            preset: 'baseline',
            confirmation,
          });
          await ctx.send('configuration.adopt', baseline);
          await ctx.reload();
        },
      }),
    );
  }
  adoptActions.append(
    h('button', {
      type: 'button', class: 'primary', text: '确认采用',
      onClick: async () => {
        const confirmation = await confirmTextDialog({
          title: '确认手动采用',
          body: '请先在游戏中手动完成这些设置，再确认采用。',
          label: '玩家确认',
          defaultValue: '我已手动设置',
          confirmLabel: '确认采用',
        });
        if (!confirmation) return;
        const payload = buildAdoptionPayload({
          recommendation,
          expectedCurrentId: ctx.workspace.current_applied_id,
          settings: recommendationSettings(recommendation),
          confirmation,
        });
        const reply = await ctx.send('configuration.adopt', payload);
        const id = reply.refs.find((ref) => ref.kind === 'AppliedConfiguration')?.id;
        if (id) ctx.remember('appliedId', id);
        await ctx.reload();
      },
    }),
  );

  root.append(
    panel('采用与下一步',
      h('p', { class: 'muted', text: '原配置与采用方案都会保留，历史页可查看父版本与差异。' }),
      adoptActions,
      h('div', { class: 'panel-actions' },
        h('button', {
          type: 'button', class: 'ghost', text: '查看游玩日志',
          onClick: () => ctx.navigate('performance'),
        }),
      ),
    ),
  );
}
