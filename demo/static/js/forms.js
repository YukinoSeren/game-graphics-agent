export function buildAdoptionPayload({ recommendation, expectedCurrentId, settings, confirmation }) {
  return {
    origin: 'recommendation',
    recommendation_id: recommendation.id,
    expected_current_id: expectedCurrentId,
    settings: { ...settings },
    preset: recommendation.template,
    confirmation,
  };
}

export function buildBaselinePayload({ expectedCurrentId, settings, preset = 'baseline', confirmation }) {
  return {
    origin: 'baseline',
    recommendation_id: null,
    expected_current_id: expectedCurrentId,
    settings: { ...settings },
    preset,
    confirmation,
  };
}

export function buildEditPayload({ expectedCurrentId, changes, changeSetId, confirmation }) {
  const payload = {
    expected_current_id: expectedCurrentId,
    changes: { ...changes },
    confirmation,
  };
  if (changeSetId) payload.change_set_id = changeSetId;
  return payload;
}

export function recommendationSettings(recommendation) {
  const values = {};
  for (const item of recommendation.settings) {
    values[item.setting_id] = item.value;
  }
  return values;
}

export function numericOrNull(value) {
  if (value === '' || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
