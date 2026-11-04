import { h } from './components.js';

export const LABELS = {
  ready: '已就绪',
  provisional: '初步可用',
  needs_review: '需要复核',
  needs_confirmation: '需要确认',
  preset: '符合当前设备',
  usable: '可使用',
  limited: '有适用限制',
  completed: '已完成',
  partial: '部分完成',
  queued: '等待开始',
  running: '进行中',
  failed: '未完成',
  cancelled: '已取消',
  interrupted: '已暂停',
  waiting_input: '等待补充信息',
  accepted: '已确认',
  rejected: '未采用',
  unknown: '待确认',
  main: '平衡方案', quality: '画质优先', performance: '流畅度优先',
  high: '高', medium: '中', low: '低', ultra: '极高',
  on: '开启', off: '关闭', fullscreen: '独占全屏', borderless: '无边框',
  smaa_t2x: 'SMAA T2X',
  true: '是',
  false: '否',
};

const PRESENTATION_REPLACEMENTS = [
  [/演示样例满足本地统计的最小样本条件。/g, '记录满足统计所需的最小样本条件。'],
  [/现代战争\s*(?:II|2)/g, '现代战争 I'],
  [/Modern Warfare\s*(?:II|2)/gi, 'Modern Warfare I'],
  [/\bMW\s*II\b/gi, '现代战争 I'],
  [/演示用的模拟说明，不是真实游戏菜单截图或官方文档。/g, '来源内容尚未由游戏菜单或官方文档核实。'],
  [/本演示预设背景/g, '当前背景'],
  [/演示预设背景/g, '当前背景'],
  [/当前演示预设背景/g, '当前背景'],
  [/演示者自定义/g, '自定义背景'],
  [/演示预设/g, '当前背景'],
  [/演示设定/g, '当前条件'],
  [/演示目标/g, '当前目标'],
  [/演示观察/g, '记录观察'],
  [/演示规则/g, '当前规则'],
  [/演示说明/g, '待核对说明'],
  [/演示资料/g, '待核验资料'],
  [/演示样例/g, '待核验资料'],
  [/演示标签/g, '固定场景标签'],
  [/演示/g, '记录'],
  [/模拟/g, '待核验'],
  [/样例/g, '记录'],
];

export function presentationText(value) {
  let text = String(value ?? '');
  for (const [pattern, replacement] of PRESENTATION_REPLACEMENTS) {
    text = text.replace(pattern, replacement);
  }
  return text;
}

export function displayValue(value, fallback = '暂无') {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map((item) => displayValue(item, '')).filter(Boolean).join('、') || fallback;
  if (typeof value === 'object') {
    return Object.entries(value)
      .map(([key, item]) => `${LABELS[key] || key}：${displayValue(item, '暂无')}`)
      .join('；');
  }
  const text = presentationText(value);
  return LABELS[text] || text;
}

function inline(parent, source) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let cursor = 0;
  for (const match of source.matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(source.slice(cursor, match.index)));
    const raw = match[0];
    parent.append(raw.startsWith('**')
      ? h('strong', { text: raw.slice(2, -2) })
      : h('code', { text: raw.slice(1, -1) }));
    cursor = match.index + raw.length;
  }
  if (cursor < source.length) parent.append(document.createTextNode(source.slice(cursor)));
}

function isRule(line) {
  return /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(line.trim());
}

function tableRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

export function renderContent(source, className = 'rich-content') {
  const root = h('div', { class: className });
  const lines = presentationText(source).replace(/\r\n?/g, '\n').split('\n');
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      root.append(h(`h${Math.min(4, heading[1].length + 2)}`, {}, inlineNode(heading[2])));
      index += 1;
      continue;
    }
    if (line.trimStart().startsWith('|') && index + 1 < lines.length && isRule(lines[index + 1])) {
      const headers = tableRow(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].trimStart().startsWith('|')) rows.push(tableRow(lines[index++]));
      const table = h('table', { class: 'content-table' },
        h('thead', {}, h('tr', {}, headers.map((cell) => h('th', {}, inlineNode(cell))))),
        h('tbody', {}, rows.map((row) => h('tr', {}, headers.map((_, column) => h('td', {}, inlineNode(row[column] || '')))))),
      );
      root.append(h('div', { class: 'table-scroll' }, table));
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const list = h('ul');
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        list.append(h('li', {}, inlineNode(lines[index++].replace(/^\s*[-*]\s+/, ''))));
      }
      root.append(list);
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const list = h('ol');
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        list.append(h('li', {}, inlineNode(lines[index++].replace(/^\s*\d+[.)]\s+/, ''))));
      }
      root.append(list);
      continue;
    }
    if (line.startsWith('>')) {
      const quote = h('blockquote');
      while (index < lines.length && lines[index].startsWith('>')) {
        const p = h('p');
        inline(p, lines[index++].replace(/^>\s?/, ''));
        quote.append(p);
      }
      root.append(quote);
      continue;
    }
    const paragraph = h('p');
    inline(paragraph, line);
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^(#{1,4})\s/.test(lines[index]) &&
      !/^\s*[-*]\s+/.test(lines[index]) && !/^\s*\d+[.)]\s+/.test(lines[index]) &&
      !lines[index].startsWith('>') && !lines[index].trimStart().startsWith('|')) {
      paragraph.append(document.createTextNode(' '));
      inline(paragraph, lines[index++]);
    }
    root.append(paragraph);
  }
  return root;
}

function inlineNode(text) {
  const node = h('span');
  inline(node, text);
  return node;
}
