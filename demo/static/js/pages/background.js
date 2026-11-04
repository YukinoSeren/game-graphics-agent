import { h, panel, badge, definitionList } from '../components.js';
import { displayValue, presentationText } from '../content.js';
import { numericOrNull } from '../forms.js';

const EDITABLE_FIELDS = [
  ['gpu_name', '显卡'],
  ['cpu', '处理器'],
  ['memory_gb', '内存（GB）', 'number'],
  ['vram_gb', '显存（GB）', 'number'],
  ['driver_version', '显卡驱动'],
  ['game_version', '游戏版本'],
  ['display_refresh_hz', '显示刷新率（Hz）', 'number'],
  ['width', '游戏分辨率宽', 'number'],
  ['height', '游戏分辨率高', 'number'],
];

function currentBackground(context) {
  const resolution = context.resolution || {};
  const desktop = context.desktop_resolution || {};
  return definitionList([
    ['处理器', context.cpu], ['显卡', context.gpu_name],
    ['内存 / 显存', context.memory_gb && context.vram_gb ? `${context.memory_gb} GB / ${context.vram_gb} GB` : null],
    ['系统 / 驱动', [context.os_name, context.driver_version].filter(Boolean).join(' / ')],
    ['桌面显示', desktop.width && desktop.height ? `${desktop.width}×${desktop.height}，${context.display_refresh_hz} Hz` : context.display_refresh_hz ? `${context.display_refresh_hz} Hz` : null],
    ['游戏渲染分辨率', `${resolution.width}×${resolution.height}`],
  ].filter(([, value]) => value));
}

function editForm(context, onSave) {
  const controls = {};
  const resolution = context.resolution || {};
  const form = h('form', { class: 'form-grid', novalidate: true },
    ...EDITABLE_FIELDS.map(([key, label, type = 'text']) => {
      const value = key === 'width' ? resolution.width
        : key === 'height' ? resolution.height : context[key];
      const control = h('input', {
        type,
        value: value === null || value === undefined ? '' : value,
        required: key === 'gpu_name',
        'aria-label': label,
      });
      controls[key] = control;
      return h('div', { class: 'field' }, h('label', { text: label }), control);
    }),
    h('p', { class: 'muted form-span', text: '这些信息用于判断建议是否适用；修改后会保留原记录。游戏范围固定为使命召唤：现代战争 I。' }),
    h('button', { type: 'submit', class: 'primary', text: '保存背景' }),
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const fields = {
      gpu_name: controls.gpu_name.value.trim(),
      cpu: controls.cpu.value.trim() || null,
      memory_gb: numericOrNull(controls.memory_gb.value),
      vram_gb: numericOrNull(controls.vram_gb.value),
      driver_version: controls.driver_version.value.trim() || null,
      game_version: controls.game_version.value.trim() || null,
      display_refresh_hz: numericOrNull(controls.display_refresh_hz.value),
    };
    const width = numericOrNull(controls.width.value);
    const height = numericOrNull(controls.height.value);
    if (width !== null && height !== null) fields.resolution = { width, height };
    await onSave(fields);
  });
  return form;
}

export async function render(root, ctx) {
  const context = await ctx.entity(ctx.workspace.context_id, 'ContextSnapshot');
  const scans = await ctx.entities('DeviceScan').catch(() => []);
  const scan = scans.filter((item) => item.recognized?.desktop_resolution?.width && item.recognized?.gpu_name === context.gpu_name).at(-1);
  const displayContext = scan ? { ...scan.recognized, ...context, os_name: context.os_name || scan.recognized.os_name, desktop_resolution: context.desktop_resolution || scan.recognized.desktop_resolution } : context;

  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('p', { class: 'eyebrow', text: '设备与游戏背景' }),
        h('h2', { text: '设备与体验目标' }),
        h('p', { text: '本机信息可在对话页检查；需要时也可以在这里修订设备与游戏设置。' }),
      ),
      badge(context.applicability === 'preset' ? '符合当前背景' : '已保存背景', 'ok'),
    ),
    panel('设备信息', currentBackground(displayContext)),
    panel('体验目标', h('p', { text: '流畅优先，尽量保留画面丰富度' }), h('p', { class: 'muted', text: '优先保留纹理细节、纹理过滤与当前渲染分辨率，先平衡阴影、环境光遮蔽和反射。' })),
    h('div', { class: 'panel-actions' }, h('button', { type: 'button', class: 'primary', text: '查看资料与依据', onClick: () => ctx.navigate('knowledge') }), h('button', { type: 'button', class: 'ghost', text: '检查本机信息', onClick: async () => { await ctx.send('device.check', {}); await ctx.reload(); } })),
    h('details', { class: 'advanced-options' }, h('summary', { text: '编辑设备与背景' }), panel('编辑信息',
      h('p', { class: 'muted', text: '设备条件改变后，请返回助手整理新方案。保存会保留原记录。' }),
      editForm(context, async (fields) => {
        try {
          await ctx.send('context.save', {
            expected_context_id: context.id,
            fields,
          });
          ctx.toast('背景已更新，请返回助手整理新方案', 'info');
          await ctx.reload();
        } catch (error) {
          ctx.toast(error.message, 'error');
        }
      }),
      h('div', { class: 'panel-actions' },
        h('button', {
          type: 'button', class: 'ghost', text: '恢复默认信息',
          onClick: async () => {
            try {
              await ctx.send('context.save', {
                expected_context_id: context.id,
                restore_preset: true,
              });
              await ctx.reload();
            } catch (error) {
              ctx.toast(error.message, 'error');
            }
          },
        }),
      ),
    )),
  );
}
