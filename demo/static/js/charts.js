const NS = 'http://www.w3.org/2000/svg';

function svg(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

export function renderFrameTimeChart(element, chart, { label = '帧时间曲线' } = {}) {
  element.replaceChildren();
  element.dataset.sourceHash = chart?.source_hash || '';
  if (!chart || !chart.points || chart.points.length === 0) {
    element.append(document.createTextNode('没有可绘制的帧时间数据。'));
    return;
  }
  const width = 760;
  const height = 240;
  const pad = { left: 52, right: 16, top: 16, bottom: 30 };
  const points = chart.points;
  const maxT = Math.max(...points.map((point) => point.t_s), 0.001);
  const maxMs = Math.max(...points.map((point) => point.ms), 50) * 1.08;
  const x = (t) => pad.left + (t / maxT) * (width - pad.left - pad.right);
  const y = (ms) => height - pad.bottom - (ms / maxMs) * (height - pad.top - pad.bottom);

  const root = svg('svg', {
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': `${label}：${points.length} 个采样点，来源哈希 ${chart.source_hash}`,
    class: 'frame-chart',
  });

  for (let i = 0; i <= 4; i += 1) {
    const value = (maxMs / 4) * i;
    const lineY = y(value);
    root.append(svg('line', {
      x1: pad.left, x2: width - pad.right, y1: lineY, y2: lineY, class: 'grid-line',
    }));
    const labelNode = svg('text', { x: 8, y: lineY + 4, class: 'axis-label' });
    labelNode.textContent = `${value.toFixed(0)} ms`;
    root.append(labelNode);
  }

  if (chart.threshold_ms !== null && chart.threshold_ms !== undefined) {
    root.append(svg('line', {
      x1: pad.left,
      x2: width - pad.right,
      y1: y(chart.threshold_ms),
      y2: y(chart.threshold_ms),
      class: 'threshold-line',
    }));
    const thresholdLabel = svg('text', {
      x: width - pad.right, y: y(chart.threshold_ms) - 6, class: 'threshold-label', 'text-anchor': 'end',
    });
    thresholdLabel.textContent = `长帧阈值 ${chart.threshold_ms} ms`;
    root.append(thresholdLabel);
  }

  const path = points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${x(point.t_s).toFixed(2)},${y(point.ms).toFixed(2)}`)
    .join(' ');
  root.append(svg('path', { d: path, class: 'chart-line' }));

  for (const point of points) {
    if (point.long) {
      root.append(svg('circle', {
        cx: x(point.t_s), cy: y(point.ms), r: 2.2, class: 'chart-long',
      }));
    }
  }

  const axis = svg('text', { x: width - pad.right, y: height - 6, class: 'axis-label', 'text-anchor': 'end' });
  axis.textContent = `${maxT.toFixed(2)} s`;
  root.append(axis);

  element.append(root);
}
