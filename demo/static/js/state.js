export function acceptSnapshot(current, incoming) {
  if (!incoming) return false;
  if (current?.scenario_id && incoming.scenario_id && current.scenario_id !== incoming.scenario_id) {
    return false;
  }
  if (current?.workspace_epoch && incoming.workspace_epoch) {
    if (incoming.workspace_epoch < current.workspace_epoch) return false;
    if (incoming.workspace_epoch > current.workspace_epoch) return true;
  }
  if (
    current?.revision !== undefined &&
    incoming.revision !== undefined &&
    incoming.revision < current.revision
  ) {
    return false;
  }
  return true;
}

export function createState() {
  return {
    scenarios: [],
    scenarioId: null,
    workspaces: new Map(),
    catalogs: new Map(),
    page: 'conversation',
    pageData: new Map(),
    lastError: null,
  };
}

export function cacheKey(scenarioId, page) {
  return `${scenarioId}:${page}`;
}
