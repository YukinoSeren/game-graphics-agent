const API_BASE = '/api/demo/v1';

export function requireCompatibleService(bootstrap) {
  const required = ['device.check', 'intent.submit', 'log.capture_recent', 'feedback.review'];
  if (bootstrap?.ui_contract !== 'conversation/2' ||
      !required.every((action) => bootstrap.capabilities?.includes(action))) {
    throw new Error('页面与当前服务版本不一致，请重新启动本地服务后刷新页面。');
  }
}

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body || {};
    this.code = body?.error?.code || 'unknown';
    this.recovery = body?.error?.recovery_actions || [];
  }
}

async function parse(response) {
  const text = await response.text();
  let body = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { error: { code: 'invalid_response', message: text } };
  }
  if (!response.ok) {
    throw new ApiError(
      body?.error?.message || `请求未完成（${response.status}）`,
      response.status,
      body,
    );
  }
  return body;
}

export function createApi(base = API_BASE) {
  async function get(path) {
    const response = await fetch(base + path, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    return parse(response);
  }

  async function entity(id, { scenario, epoch, kind } = {}) {
    const params = new URLSearchParams({
      scenario_id: scenario,
      workspace_epoch: String(epoch),
    });
    if (kind) params.set('kind', kind);
    const body = await get(`/entities/${encodeURIComponent(id)}?${params}`);
    return body.entity;
  }

  async function entities(kind, { scenario, epoch } = {}) {
    const params = new URLSearchParams({
      scenario_id: scenario,
      workspace_epoch: String(epoch),
      kind,
    });
    const body = await get(`/entities?${params}`);
    return body.entities;
  }

  async function task(id, { scenario, epoch } = {}) {
    const params = new URLSearchParams({
      scenario_id: scenario,
      workspace_epoch: String(epoch),
    });
    const body = await get(`/tasks/${encodeURIComponent(id)}?${params}`);
    return body.task;
  }

  async function command({ requestId, scenarioId, epoch, revision, action, payload }) {
    const response = await fetch(base + '/commands', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: requestId,
        scenario_id: scenarioId,
        workspace_epoch: epoch,
        expected_revision: revision,
        action,
        payload: payload || {},
      }),
    });
    return parse(response);
  }

  async function upload({ requestId, scenarioId, epoch, revision, file, filename, mediaType }) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const hash = Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('');
    const media = mediaType || file.type || 'application/octet-stream';
    const command = {
      request_id: requestId,
      scenario_id: scenarioId,
      workspace_epoch: epoch,
      expected_revision: revision,
      action: 'file.upload',
      payload: {
        media_type: media,
        filename: filename || file.name,
        size: bytes.length,
        content_hash: hash,
      },
    };
    const form = new FormData();
    form.append('command', new Blob([JSON.stringify(command)], { type: 'application/json' }));
    form.append('file', file, command.payload.filename);
    const response = await fetch(base + '/uploads', { method: 'POST', body: form });
    return parse(response);
  }

  async function history({ scenario, epoch, after = 0, limit = 60 }) {
    const params = new URLSearchParams({
      scenario_id: scenario,
      workspace_epoch: String(epoch),
      after: String(after),
      limit: String(limit),
    });
    return get(`/history?${params}`);
  }

  async function recentLogs({ scenario, epoch }) {
    const params = new URLSearchParams({ scenario_id: scenario, workspace_epoch: String(epoch) });
    return (await get(`/recent-play-logs?${params}`)).logs;
  }

  async function fileUrl(id, { scenario, epoch }) {
    const params = new URLSearchParams({
      scenario_id: scenario,
      workspace_epoch: String(epoch),
    });
    return `${base}/files/${encodeURIComponent(id)}?${params}`;
  }

  return { get, entity, entities, task, command, upload, history, fileUrl, recentLogs };
}
