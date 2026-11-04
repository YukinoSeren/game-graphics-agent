import { h, emptyState } from '../components.js';
import { presentationText } from '../content.js';

const STEPS = [['background', '设备与背景'], ['knowledge', '资料与依据'], ['recommendation', '设置详情'], ['performance', '游玩日志'], ['feedback', '体验反馈']];

export async function render(root, ctx) {
  root.append(h('div', { class: 'page-heading-compact' }, h('p', { class: 'eyebrow', text: '游戏与进度' }), h('h2', { text: '我的游戏' })));
  const cards = await ctx.entities('GameCard').catch(() => []);
  if (!cards.length) {
    root.append(emptyState('这里还没有游戏。告诉我你想玩什么，资料整理完成后就会出现在这里。', h('button', { type: 'button', class: 'primary', text: '告诉助手', onClick: () => ctx.navigate('conversation') })));
    return;
  }
  const analyses = await ctx.entities('FeedbackAnalysis').catch(() => []);
  const list = h('div', { class: 'game-list' });
  for (const card of cards) {
    ctx.remember('knowledgeId', card.knowledge_id);
    const analysis = analyses.filter((item) => item.created_at >= (card.intent_created_at || card.created_at)).at(-1);
    const snapshot = await ctx.entity(card.knowledge_id, 'KnowledgeSnapshot').catch(() => null);
    const sourceCount = snapshot?.source_ids?.length || 0;
    const context = await ctx.entity(card.context_id, 'ContextSnapshot').catch(() => null);
    list.append(h('article', { class: 'game-card' },
      h('div', { class: 'game-card-main' },
        h('div', { class: 'game-card-copy' },
          h('span', { class: 'game-card-status', text: `${sourceCount} 份资料 · ${card.recommendation_id ? '设置方案已就绪' : '请返回助手整理设置方案'}` }),
          h('h3', { text: presentationText(card.game_name) }),
          context?.gpu_name ? h('p', { class: 'game-card-device', text: context.gpu_name }) : null,
          h('p', { class: 'game-card-experience', text: '流畅优先，尽量保留画面丰富度' }),
          analysis ? h('p', { class: 'muted', text: analysis.status === 'completed' ? '体验分析已完成，可查看调整建议。' : ['queued', 'running'].includes(analysis.status) ? '正在重新分析体验反馈。' : '体验分析记录已保留。' }) : null,
        ),
        h('button', { type: 'button', class: 'primary', text: '查看设备与背景', onClick: () => ctx.navigate('background') }),
      ),
      h('nav', { class: 'game-card-links', 'aria-label': '游戏体验流程' }, STEPS.map(([page, label], index) => [index ? h('span', { class: 'flow-arrow', 'aria-hidden': 'true', text: '→' }) : null, h('button', { type: 'button', class: 'text-button', text: label, onClick: () => ctx.navigate(page) })])),
    ));
  }
  root.append(list);
}
