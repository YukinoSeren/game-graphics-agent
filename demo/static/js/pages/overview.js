import {
  h, panel, badge, stat, alertBox, drawer, definitionList, emptyState,
  formatNumber, keyValueTable, readinessBadge,
} from '../components.js';
import { numericOrNull } from '../forms.js';

function caseCard(scenario, active, onClick) {
  return h(
    'button',
    {
      type: 'button',
      class: `scenario-card ${active ? 'active' : ''}`,
      onClick,
      'aria-pressed': active ? 'true' : 'false',
    },
    h('h3', { text: scenario.title }),
    h('p', { class: 'game', text: `${scenario.gpu_name} · ${scenario.game_name}` }),
    h('span', { class: 'tag', text: '演示案例' }),
    definitionList([
      ['演示分辨率', `${scenario.resolution.width}×${scenario.resolution.height}`],
      ['叙事', scenario.narrative],
      ['工作区', `epoch ${scenario.workspace_epoch} · revision ${scenario.revision}`],
    ]),
  );
}

function backgroundForm(context, onSubmit) {
  const fields = {};
  const input = (key, label, value, type = 'text') => {
    const element = h('input', {
      type,
      value: value === null || value === undefined ? '' : value,
      'aria-label': label,
    });
    fields[key] = element;
    return h('div', { class: 'field' }, h('label', { text: label }), element);
  };
  const resolution = context.resolution || {};
  // A real form element: the save control is a native submit button and the
  // submit handler performs the revision write.
  const form = h(
    'form',
    { class: 'form-grid', novalidate: true },
    input('gpu_name', '显卡（玩家声明）', context.gpu_name),
    input('game_name', '游戏（玩家声明）', context.game_name),
    input('cpu', 'CPU', context.cpu),
    input('memory_gb', '内存 GB', context.memory_gb, 'number'),
    input('vram_gb', '显存 GB', context.vram_gb, 'number'),
    input('driver_version', '驱动版本', context.driver_version),
    input('game_version', '游戏版本', context.game_version),
    input('display_refresh_hz', '刷新率 Hz', context.display_refresh_hz, 'number'),
    input('res_width', '分辨率宽', resolution.width, 'number'),
    input('res_height', '分辨率高', resolution.height, 'number'),
    h('button', { type: 'submit', class: 'primary', text: '保存背景修订' }),
  );
  const submit = async (event) => {
    event.preventDefault();
    const values = {
      game_name: fields.game_name.value.trim(),
      gpu_name: fields.gpu_name.value.trim(),
      cpu: fields.cpu.value.trim() || null,
      memory_gb: numericOrNull(fields.memory_gb.value),
      vram_gb: numericOrNull(fields.vram_gb.value),
      driver_version: fields.driver_version.value.trim() || null,
      game_version: fields.game_version.value.trim() || null,
      display_refresh_hz: numericOrNull(fields.display_refresh_hz.value),
    };
    const width = numericOrNull(fields.res_width.value);
    const height = numericOrNull(fields.res_height.value);
    if (width && height) values.resolution = { width, height };
    await onSubmit(values);
  };
  form.addEventListener('submit', submit);
  return {
    form,
    fields,
  };
}

function deviceDetails(device) {
  const desktop = device.desktop_resolution;
  return definitionList([
    ['处理器', device.cpu],
    ['显卡', device.gpu_name],
    ['内存', `${device.memory_gb} GB`],
    ['显存', `${device.vram_gb} GB`],
    ['操作系统', device.os_name],
    ['显卡驱动', device.driver_version],
    ['桌面显示', desktop ? `${desktop.width} × ${desktop.height} · ${device.display_refresh_hz} Hz` : null],
  ]);
}

async function devicePanel(ctx) {
  const results = h('div', { 'aria-live': 'polite' });
  const savedId = ctx.recall('deviceCheckId');
  const saved = savedId ? await ctx.entity(savedId, 'DeviceScan').catch(() => null) : null;
  results.append(saved?.recognized
    ? deviceDetails(saved.recognized)
    : h('p', { class: 'muted', text: '查看处理器、显卡、内存和显示配置。' }));
  const inspect = h('button', {
    type: 'button',
    class: 'primary',
    text: '检查本机信息',
    onClick: async () => {
      inspect.disabled = true;
      inspect.textContent = '正在检查…';
      try {
        const reply = await ctx.send('device.check', {});
        const id = reply.refs.find((ref) => ref.kind === 'DeviceScan')?.id;
        if (id) ctx.remember('deviceCheckId', id);
        results.replaceChildren(deviceDetails(reply.data.recognized));
        await ctx.reload();
      } catch (error) {
        results.replaceChildren(alertBox(error.message));
      } finally {
        inspect.disabled = false;
        inspect.textContent = '检查本机信息';
      }
    },
  });
  return panel('本机信息',
    h('div', { class: 'panel-actions' }, inspect),
    results,
  );
}

export async function render(root, ctx) {
  const workspace = ctx.workspace;
  const context = await ctx.entity(workspace.context_id);
  const catalog = ctx.catalog;
  const currentApplied = workspace.current_applied_id
    ? await ctx.entity(workspace.current_applied_id, 'AppliedConfiguration').catch(() => null)
    : null;

  root.append(
    h(
      'div',
      { class: 'page-head' },
      h('div', {},
        h('p', { class: 'eyebrow', text: '概览与设备' }),
        h('h2', { text: context.game_name }),
        h('p', {
          text: `${context.gpu_name} · 游戏输出 ${context.resolution.width}×${context.resolution.height}`,
        }),
      ),
      readinessBadge(context.applicability),
    ),
  );

  root.append(await devicePanel(ctx));

  root.append(
    h('div', { class: 'grid two' },
      ...ctx.state.scenarios.map((scenario, index) =>
        h('div', { class: 'panel' },
          caseCard(
            scenario,
            scenario.scenario_id === ctx.scenarioId,
            () => ctx.selectScenario(scenario.scenario_id),
          ),
        ),
      ),
    ),
  );

  root.append(
    h('div', { class: 'grid two' },
      panel('当前背景（玩家声明与演示预设）',
        definitionList([
          ['案例 ID', ctx.scenarioId],
          ['显卡', context.gpu_name],
          ['游戏', context.game_name],
          ['输出分辨率', `${context.resolution.width}×${context.resolution.height}`],
          ['CPU', context.cpu ?? '未知'],
          ['内存', context.memory_gb ? `${context.memory_gb} GB` : '未知'],
          ['显存', context.vram_gb ? `${context.vram_gb} GB` : '未知'],
          ['驱动', context.driver_version ?? '未知'],
          ['游戏版本', context.game_version ?? '未知'],
          ['适用性', context.applicability],
          ['修订', String(context.revision)],
        ]),
        h('div', { class: 'panel-actions' },
          h('button', {
            type: 'button',
            class: 'ghost',
            text: '恢复案例预设',
            onClick: async () => {
              await ctx.send('context.save', {
                expected_context_id: context.id,
                restore_preset: true,
              });
              await ctx.reload();
            },
          }),
        ),
        h('p', { class: 'muted', text: `未知字段：${(context.unknown_fields || []).join('、') || '无'}` }),
      ),
      panel('当前采用配置',
        currentApplied
          ? h('div', {},
            definitionList([
              ['版本 ID', currentApplied.id],
              ['来源', currentApplied.origin],
              ['父版本', currentApplied.parent_id ?? '无'],
              ['确认', currentApplied.confirmation],
              ['设置项', String(Object.keys(currentApplied.settings).length)],
              ['shadow/reflection', currentApplied.settings['mw2.shadow_quality'] ?? currentApplied.settings['fh6.reflection_quality'] ?? '未知'],
            ]),
          )
          : emptyState('还没有保存或采用的配置版本。可在“画质方案”页保存原配置并确认采用。'),
      ),
    ),
  );

  const formHolder = h('div', {});
  const form = backgroundForm(context, async (values) => {
    await ctx.send('context.save', {
      expected_context_id: context.id,
      fields: values,
    });
    await ctx.reload();
  });
  formHolder.append(form.form);
  root.append(
    panel('编辑背景（新建修订）',
      h('p', { class: 'muted', text: '每次保存生成新的背景修订；旧推荐不会被自动标为适用。' }),
      formHolder,
    ),
  );

  const requestId = ctx.recall('informationRequestId');
  let request = null;
  if (requestId) {
    request = await ctx.entity(requestId, 'InformationRequest').catch(() => null);
    if (request && request.status !== 'open') request = null;
  }

  root.append(
    panel('补充设备信息',
      h('div', { class: 'panel-actions' },
        h('button', {
          type: 'button',
          class: 'ghost',
          text: '补充其他信息',
          onClick: async () => {
            const reply = await ctx.send('device.scan', {});
            const id = reply.refs.find((ref) => ref.kind === 'InformationRequest')?.id;
            if (id) ctx.remember('informationRequestId', id);
            await ctx.reload();
          },
        }),
        h('button', {
          type: 'button',
          class: 'ghost',
          text: '进入知识采集',
          onClick: () => ctx.navigate('knowledge'),
        }),
      ),
      request ? questionForm(request, context, ctx) : null,
    ),
  );
}

function questionForm(request, context, ctx) {
  const answers = {};
  const skipped = [];
  const rows = request.fields
    .filter((field) => !(field.field in (request.answers || {})) && !request.skipped_fields.includes(field.field))
    .map((field) => {
      const input = h('input', { type: 'text', 'aria-label': field.prompt });
      const skip = h('input', { type: 'checkbox' });
      answers[field.field] = input;
      skipped.push({ field: field.field, checkbox: skip });
      return h('div', { class: 'field-row' },
        h('div', { class: 'field' }, h('label', { text: field.prompt }), input),
        h('label', { class: 'inline' }, skip, '不知道/跳过'),
      );
    });
  if (!rows.length) {
    return h('p', { class: 'muted', text: '当前补充问题已处理。' });
  }
  const submit = async () => {
    const values = {};
    const skippedFields = [];
    for (const [field, input] of Object.entries(answers)) {
      const value = input.value.trim();
      if (value) values[field] = ['memory_gb', 'vram_gb', 'display_refresh_hz'].includes(field)
        ? Number(value)
        : value;
    }
    for (const item of skipped) {
      if (item.checkbox.checked) skippedFields.push(item.field);
    }
    await ctx.send('information.answer', {
      request_id: request.id,
      question_revision: request.revision,
      context_revision: context.revision,
      answers: values,
      skipped_fields: skippedFields,
    });
    ctx.remember('informationRequestId', request.id);
    await ctx.reload();
  };
  return h('div', { class: 'stack' },
    ...rows,
    h('div', { class: 'panel-actions' },
      h('button', { type: 'button', class: 'primary', text: '保存回答', onClick: submit }),
    ),
    h('p', { class: 'muted', text: '回答是玩家声明；不会变成自动探测结果。' }),
  );
}
