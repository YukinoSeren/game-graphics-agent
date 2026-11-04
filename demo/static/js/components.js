const VALUE_LABELS = {
  ready: '已就绪', provisional: '初步可用', needs_review: '需要复核',
  needs_confirmation: '需要确认', preset: '符合当前设备', usable: '可使用',
  limited: '有适用限制', completed: '已完成', partial: '部分完成', queued: '等待开始',
  running: '进行中', failed: '未完成', cancelled: '已取消', interrupted: '已暂停',
  waiting_input: '等待补充信息', accepted: '已确认', rejected: '未采用', unknown: '待确认',
  knowledge_ready: '资料已就绪', needs_experience: '等待体验需求', needs_game: '等待游戏名称',
  researching: '正在整理资料', unsupported: '暂不支持',
  pending: '待处理', done: '已完成', open: '待处理', resolved: '已补充',
  fixture: '资料', simulated: '记录', demo_seed: '历史记录', user_declared: '你提供的信息',
  true: '是', false: '否',
};

function plainValue(value) {
  if (value === null || value === undefined || value === '') return '暂无';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map(plainValue).filter((item) => item !== '暂无').join('、') || '暂无';
  if (typeof value === 'object') {
    return Object.entries(value).map(([key, item]) => `${key}：${plainValue(item)}`).join('；');
  }
  const text = String(value);
  return VALUE_LABELS[text] || text;
}

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = plainValue(value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key in node && key !== 'list') {
      node[key] = value;
    } else {
      node.setAttribute(key, value === true ? '' : value);
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(plainValue(child)));
  }
  return node;
}

export function badge(label, tone = '') {
  return h('span', { class: `badge ${tone}`, text: plainValue(label) });
}

export function panel(title, ...children) {
  return h('section', { class: 'panel' }, title ? h('h3', { text: title }) : null, ...children);
}

export function stat(label, value, hint) {
  return h(
    'div',
    { class: 'stat' },
    h('span', { class: 'stat-label', text: label }),
    h('strong', { class: 'stat-value', text: value ?? '未知' }),
    hint ? h('span', { class: 'stat-hint', text: hint }) : null,
  );
}

export function emptyState(message, action) {
  return h('div', { class: 'empty-state' }, h('p', { text: message }), action || null);
}

export function alertBox(message, tone = 'error') {
  return h('div', { class: `alert ${tone}`, role: 'alert', text: message });
}

export function formatNumber(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '未知';
  return Number(value).toFixed(digits);
}

export function formatMs(value) {
  return value === null || value === undefined ? '未知' : `${formatNumber(value, 1)} ms`;
}

const TONES = {
  ready: 'ok',
  provisional: 'warn',
  needs_review: 'warn',
  limited: 'warn',
  usable: 'ok',
};

export function readinessBadge(readiness) {
  return badge(VALUE_LABELS[readiness] || readiness, TONES[readiness] || '');
}

export function toast(message, tone = 'info') {
  let holder = document.getElementById('toast-holder');
  if (!holder) {
    holder = h('div', { id: 'toast-holder', class: 'toast-holder', role: 'status' });
    document.body.append(holder);
  }
  const item = h('div', { class: `toast ${tone}`, text: message });
  holder.append(item);
  setTimeout(() => item.remove(), 4200);
}

export function confirmDialog({ title, body, confirmLabel = '确认', cancelLabel = '取消' }) {
  return new Promise((resolve) => {
    const dialog = h('dialog', { class: 'modal' });
    const close = (value) => {
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    dialog.append(
      h('h3', { text: title }),
      h('p', { class: 'modal-body', text: body }),
      h(
        'div',
        { class: 'modal-actions' },
        h('button', { type: 'button', class: 'ghost', text: cancelLabel, onClick: () => close(false) }),
        h('button', { type: 'button', class: 'primary', text: confirmLabel, onClick: () => close(true) }),
      ),
    );
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function drawer(title, content) {
  const existing = document.querySelector('.drawer');
  if (existing) existing.remove();
  const trigger = document.activeElement;
  const close = () => {
    node.remove();
    if (trigger?.isConnected) trigger.focus();
  };
  const node = h(
    'aside',
    { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h(
      'div',
      { class: 'drawer-head' },
      h('h3', { text: title }),
      h('button', { type: 'button', class: 'ghost', text: '关闭', onClick: close }),
    ),
    h('div', { class: 'drawer-body' }, content),
  );
  document.body.append(node);
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const focusable = [...node.querySelectorAll('button, a[href], input, select, textarea, [tabindex]')].filter((item) => !item.disabled && item.tabIndex !== -1);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  node.querySelector('button').focus();
  return node;
}

export function definitionList(entries) {
  const dl = h('dl', { class: 'definition-list' });
  for (const [key, value] of entries) {
    dl.append(h('dt', { text: key }), h('dd', { text: plainValue(value) }));
  }
  return dl;
}

export function keyValueTable(columns, rows) {
  return h(
    'div',
    { class: 'table-scroll' },
    h(
      'table',
      {},
      h('thead', {}, h('tr', {}, columns.map((column) => h('th', { text: column.label })))),
      h(
        'tbody',
        {},
        rows.map((row) =>
          h('tr', {}, columns.map((column) => h('td', {}, column.render ? column.render(row) : plainValue(row[column.key])))),
        ),
      ),
    ),
  );
}
