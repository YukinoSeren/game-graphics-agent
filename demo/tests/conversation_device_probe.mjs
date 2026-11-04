import assert from 'node:assert/strict';

class Element {
  constructor() { this.children = []; this.value = ''; this.disabled = false; this._text = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  addEventListener() {}
  get firstChild() { return this.children[0]; }
  get textContent() { return this._text + this.children.map((child) => child.textContent || '').join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
}
globalThis.Node = Element;
globalThis.document = {
  createElement: () => new Element(),
  createTextNode(text) { const node = new Element(); node.textContent = text; return node; },
};

const { render } = await import('../static/js/pages/conversation.js');
const full = { id: 'checked-device', recognized: {
  gpu_name: 'RTX 4060 Laptop GPU', cpu: 'i7-14650HX', memory_gb: 16, vram_gb: 8,
  display_refresh_hz: 165, os_name: 'Windows 11', driver_version: '621.01',
  desktop_resolution: { width: 2560, height: 1600 },
} };
const partial = { id: 'information-scan', recognized: { gpu_name: 'RTX 4060 Laptop GPU' } };
for (const scans of [[full, partial], [partial]]) {
  const root = new Element();
  await render(root, {
    entities: async (kind) => kind === 'DeviceScan' ? scans : [],
    remember() {},
  });
  assert.match(root.textContent, /检查本机信息/);
  assert.doesNotMatch(root.textContent, /undefined|\[object Object\]/);
  if (scans.length === 2) assert.match(root.textContent, /16 GB/);
}
console.log('conversation remains usable after an incomplete information scan');

const visibleAt = new Date(Date.now() + 3000).toISOString();
const root = new Element();
let scheduled;
await render(root, {
  entities: async (kind) => kind === 'ConversationMessage'
    ? [{role: 'user', content: '现代战争1'}, {role: 'assistant', content: '你希望得到怎样的体验？', visible_at: visibleAt}]
    : kind === 'GameIntent' ? [{status: 'needs_experience', reply_visible_at: visibleAt}] : [],
  remember() {}, scheduleRefreshAt(value) { scheduled = value; },
});
assert.match(root.textContent, /现代战争1/);
assert.doesNotMatch(root.textContent, /你希望得到怎样的体验/);
assert.equal(scheduled, visibleAt);
