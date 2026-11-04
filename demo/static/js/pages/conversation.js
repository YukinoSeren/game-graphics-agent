import { h, alertBox } from '../components.js';
import { presentationText, renderContent } from '../content.js';

const GAME_CARD_PATH = 'library';

async function deviceSummary(ctx) {
  const scans = await ctx.entities('DeviceScan').catch(() => []);
  // Information follow-ups also create DeviceScan rows, but contain only a
  // few fields. Keep the latest complete check instead of treating them as
  // a full machine profile.
  const scan = scans.filter((item) => item.recognized?.desktop_resolution?.width &&
    item.recognized?.desktop_resolution?.height && item.recognized?.memory_gb).at(-1) || null;
  if (!scan?.recognized) return null;
  ctx.remember('deviceCheckId', scan.id);
  const device = scan.recognized;
  const summary = h('div', { class: 'device-summary' },
    h('span', { class: 'device-dot', 'aria-hidden': 'true' }),
    h('span', { text: `${device.gpu_name} · ${device.memory_gb} GB 内存 · ${device.display_refresh_hz} Hz` }),
  );
  const details = h('details', { class: 'device-details' },
    h('summary', { text: '查看设备详情' }),
    h('dl', { class: 'device-list' },
      h('dt', { text: '处理器' }), h('dd', { text: device.cpu }),
      h('dt', { text: '显卡' }), h('dd', { text: device.gpu_name }),
      h('dt', { text: '内存' }), h('dd', { text: `${device.memory_gb} GB` }),
      h('dt', { text: '显存' }), h('dd', { text: `${device.vram_gb} GB` }),
      h('dt', { text: '操作系统' }), h('dd', { text: device.os_name }),
      h('dt', { text: '显卡驱动' }), h('dd', { text: device.driver_version }),
      h('dt', { text: '桌面显示' }), h('dd', { text: `${device.desktop_resolution.width}×${device.desktop_resolution.height} · ${device.display_refresh_hz} Hz` }),
    ),
  );
  return h('div', { class: 'device-ready' }, summary, details);
}

async function renderMessages(ctx, host) {
  const messages = await ctx.entities('ConversationMessage').catch(() => []);
  const intents = await ctx.entities('GameIntent').catch(() => []);
  const latestIntent = intents.at(-1) || null;
  const now = Date.now();
  const pending = messages.filter((message) => message.role === 'assistant' &&
    message.visible_at && new Date(message.visible_at).getTime() > now);
  if (!messages.length) {
    host.append(h('div', { class: 'conversation-welcome' },
      h('span', { class: 'welcome-index', text: '先说说你的想法' }),
      h('p', { text: '告诉我你想玩的游戏，以及希望画面和操作有什么变化。' }),
    ));
  } else {
    for (const message of messages) {
      if (message.visible_at && new Date(message.visible_at).getTime() > now) continue;
      const bubble = h('article', { class: `message ${message.role === 'user' ? 'from-user' : 'from-assistant'}` },
        h('span', { class: 'message-author', text: message.role === 'user' ? '你' : '助手' }),
        renderContent(message.content, 'message-content'),
      );
      host.append(bubble);
    }
  }

  if (pending.length) {
    const nextReply = pending.map((message) => message.visible_at).sort()[0];
    ctx.scheduleRefreshAt?.(nextReply);
    host.append(h('div', { class: 'assistant-thinking', role: 'status', 'aria-label': '助手正在思考' },
      h('span', { class: 'thinking-dot' }), h('span', { class: 'thinking-dot' }), h('span', { class: 'thinking-dot' }),
    ));
    if (latestIntent?.task_id) ctx.trackTask(latestIntent.task_id);
    return latestIntent;
  }

  if (latestIntent?.status === 'needs_experience' || latestIntent?.status === 'needs_game') {
    host.append(h('p', { class: 'inline-hint', text: latestIntent.status === 'needs_game' ? '补充游戏的完整名称即可。' : '补充你希望改善的体验即可。' }));
  } else if (latestIntent?.status === 'researching' && latestIntent.task_id) {
    const task = await ctx.api.task(latestIntent.task_id, {
      scenario: ctx.scenarioId,
      epoch: ctx.workspace.workspace_epoch,
    }).catch(() => null);
    if (task && ['queued', 'running'].includes(task.status)) {
      ctx.trackTask(task.id);
      host.append(h('div', { class: 'research-progress', role: 'status' },
        h('span', { class: 'progress-indicator', 'aria-hidden': 'true' }),
        h('div', {},
          h('strong', { text: '正在检索与收集资料' }),
          h('p', { text: '正在整理游戏设置、设备条件与画质取舍，预计需要约 1 分钟。' }),
          h('p', { class: 'research-stages', text: '收集资料 → 整理依据 → 生成设置方案' }),
          h('div', { class: 'research-meter', 'aria-hidden': 'true' },
            h('span', { style: `transform:scaleX(${latestIntent.card_not_before ? Math.max(0.03, Math.min(0.98, 1 - (new Date(latestIntent.card_not_before).getTime() - now) / 60000)) : 0.1})` }),
          ),
        ),
      ));
    } else if (task && ['failed', 'cancelled', 'interrupted', 'partial'].includes(task.status)) {
      host.append(h('div', { class: 'research-progress' },
        h('div', {},
          h('strong', { text: task.status === 'partial' ? '资料还不完整' : '资料整理中断' }),
          h('p', { text: '已完成的资料已保留。可以继续整理或稍后重试。' }),
        ),
        h('button', {
          type: 'button', class: 'ghost', text: '继续整理',
          onClick: async () => {
            const reply = await ctx.send('task.resume', { task_id: task.id });
            if (reply.task_id) ctx.trackTask(reply.task_id);
            await ctx.reload();
          },
        }),
      ));
    }
  } else if (latestIntent?.status === 'ready') {
    const card = (await ctx.entities('GameCard').catch(() => [])).at(-1);
    const snapshot = card?.knowledge_id ? await ctx.entity(card.knowledge_id, 'KnowledgeSnapshot').catch(() => null) : null;
    const complete = snapshot?.source_ids?.length === 15 && Boolean(card?.recommendation_id);
    host.append(h('article', { class: 'conversation-game-card' },
      h('div', {},
        h('span', { class: 'game-card-status', text: complete ? '资料与方案已就绪' : '已有资料记录' }),
        h('h3', { text: presentationText(latestIntent.game_name) }),
        h('p', { text: complete ? '已整理 15 份资料并生成设置方案，这款游戏已加入“我的游戏”。' : '游戏记录已保留，可进入“我的游戏”查看。' }),
      ),
      h('button', { type: 'button', class: 'primary', text: '查看我的游戏', onClick: () => ctx.navigate(GAME_CARD_PATH) }),
    ));
  }

  return latestIntent;
}

export async function render(root, ctx) {
  root.append(h('section', { class: 'conversation-page' },
    h('div', { class: 'conversation-heading' },
      h('p', { class: 'eyebrow', text: '你的游戏体验' }),
      h('h2', { text: '你想玩什么游戏，希望得到怎样的体验？' }),
    ),
  ));

  const thread = h('section', { class: 'conversation-thread', 'aria-live': 'polite', 'aria-label': '对话内容' });
  const latestIntent = await renderMessages(ctx, thread);
  root.firstChild.append(thread);

  const input = h('textarea', {
    rows: 3,
    maxLength: 2000,
    value: ctx.recall?.('conversationDraft') || '',
    placeholder: latestIntent?.status === 'needs_game'
      ? '例如：使命召唤：现代战争 I，希望交战时少卡顿'
      : latestIntent?.status === 'needs_experience'
        ? '例如：交战时少卡顿，同时保持画面清晰'
      : '例如：我想玩一款射击游戏，希望画面清晰、交战时少卡顿',
    'aria-label': '游戏和体验需求',
  });
  const waiting = latestIntent?.status === 'researching' ||
    new Date(latestIntent?.reply_visible_at || 0).getTime() > Date.now();
  const send = h('button', { type: 'submit', class: 'primary conversation-send', text: '发送', disabled: waiting });
  input.addEventListener('input', () => ctx.remember('conversationDraft', input.value));
  const device = await deviceSummary(ctx);
  const deviceState = h('div', { class: 'device-inline', 'aria-live': 'polite' },
    device || h('span', { class: 'muted', text: '设备信息尚未检查' }),
  );
  const check = h('button', {
    type: 'button', class: 'ghost device-check-button', text: '检查本机信息',
    onClick: async () => {
      check.disabled = true;
      check.textContent = '正在检查…';
      deviceState.replaceChildren(h('span', { class: 'muted', text: '正在检查本机信息…' }));
      try {
        const reply = await ctx.send('device.check', {}, { quiet: true });
        const id = reply.refs.find((ref) => ref.kind === 'DeviceScan')?.id;
        if (id) ctx.remember('deviceCheckId', id);
        await ctx.reload();
      } catch (error) {
        deviceState.replaceChildren(alertBox(error.message));
      } finally {
        check.disabled = false;
        check.textContent = '检查本机信息';
      }
    },
  });
  const form = h('form', { class: 'composer' },
    input,
    h('div', { class: 'composer-footer' },
      h('div', { class: 'composer-tools' }, deviceState, check),
      send,
    ),
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (waiting || send.disabled) return;
    const message = input.value.trim();
    if (!message) { input.focus(); return; }
    send.disabled = true;
    send.textContent = '正在发送…';
    try {
      const reply = await ctx.send('intent.submit', {
        message,
        ...(latestIntent?.status === 'needs_experience' || latestIntent?.status === 'needs_game' ? { reply_to: latestIntent.id } : {}),
      }, { quiet: true });
      input.value = '';
      ctx.remember('conversationDraft', '');
      if (reply.task_id) ctx.trackTask(reply.task_id);
      await ctx.reload();
    } catch (error) {
      thread.append(alertBox(error.message));
    } finally {
      send.disabled = false;
      send.textContent = '发送';
    }
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  root.firstChild.append(form);
}
