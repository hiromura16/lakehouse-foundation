/** Preparation-only contracts. A profile selection is never runtime evidence. */
import { api } from './api.js';

export interface SetupStep {
  id: string;
  title: string;
  description: string;
  kind: 'configuration' | 'host' | 'build' | 'bootstrap' | 'validation' | 'access';
}
export interface SetupComponent {
  id: string;
  title: string;
  description: string;
  profile: string;
  selected: boolean;
  configured: boolean;
  missing_settings: string[];
  management_supported: boolean;
  required_profiles: string[];
  depends_on: string[];
  steps: SetupStep[];
  state:
    'ready_for_operator_review' | 'configuration_required' | 'selected' | 'operator_setup_required';
  external_link_configured: boolean;
}
export interface SetupView {
  version: 1;
  manager: 'operator-compose';
  apply_supported: false;
  applied_profiles: string[];
  desired_profiles: string[];
  pending_restart: boolean;
  components: SetupComponent[];
}
export interface SetupPlan {
  version: 1;
  component_id: string;
  title: string;
  profiles_before: string[];
  profiles_after: string[];
  settings_to_review: string[];
  steps: SetupStep[];
  can_apply: false;
  requires_operator: true;
  notice: string;
}

const identifier = (value: unknown): value is string =>
  typeof value === 'string' && /^[a-z][a-z0-9-]{0,48}$/.test(value);
const text = (value: unknown): value is string =>
  typeof value === 'string' && value.length > 0 && value.length <= 2048;
const strings = (value: unknown, maximum = 40): value is string[] =>
  Array.isArray(value) && value.length <= maximum && value.every(text);
const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const steps = (value: unknown): value is SetupStep[] =>
  Array.isArray(value) &&
  value.length <= 20 &&
  value.every(
    (step) =>
      record(step) &&
      identifier(step.id) &&
      text(step.title) &&
      text(step.description) &&
      typeof step.kind === 'string' &&
      ['configuration', 'host', 'build', 'bootstrap', 'validation', 'access'].includes(
        String(step.kind),
      ),
  );

export function isSetupView(value: unknown): value is SetupView {
  return (
    record(value) &&
    value.version === 1 &&
    value.manager === 'operator-compose' &&
    value.apply_supported === false &&
    strings(value.applied_profiles) &&
    strings(value.desired_profiles) &&
    typeof value.pending_restart === 'boolean' &&
    Array.isArray(value.components) &&
    value.components.length <= 20 &&
    value.components.every(
      (component) =>
        record(component) &&
        identifier(component.id) &&
        text(component.title) &&
        text(component.description) &&
        identifier(component.profile) &&
        typeof component.selected === 'boolean' &&
        typeof component.configured === 'boolean' &&
        strings(component.missing_settings) &&
        typeof component.management_supported === 'boolean' &&
        strings(component.required_profiles) &&
        strings(component.depends_on) &&
        steps(component.steps) &&
        typeof component.external_link_configured === 'boolean' &&
        typeof component.state === 'string' &&
        [
          'ready_for_operator_review',
          'configuration_required',
          'selected',
          'operator_setup_required',
        ].includes(String(component.state)),
    ) &&
    new Set(value.components.map((component) => component.id)).size === value.components.length
  );
}

export function isSetupPlan(value: unknown): value is SetupPlan {
  return (
    record(value) &&
    value.version === 1 &&
    identifier(value.component_id) &&
    text(value.title) &&
    strings(value.profiles_before) &&
    strings(value.profiles_after) &&
    strings(value.settings_to_review) &&
    steps(value.steps) &&
    value.can_apply === false &&
    value.requires_operator === true &&
    text(value.notice)
  );
}

/** No arbitrary command, path, YAML or database connection is accepted. */
export async function prepareSetup(componentId: string): Promise<SetupPlan> {
  if (!identifier(componentId)) throw new Error('追加する機能を選び直してください。');
  const result = await api<unknown>('/setup/plan', {
    method: 'POST',
    payload: { component_id: componentId },
  });
  if (!isSetupPlan(result) || result.component_id !== componentId)
    throw new Error('準備計画の形式を確認できません。実行せずAPIの版を確認してください。');
  // Export only this public contract. An unexpected backend field must never
  // silently enter an operator download (for example a private setting value).
  return {
    version: 1,
    component_id: result.component_id,
    title: result.title,
    profiles_before: [...result.profiles_before],
    profiles_after: [...result.profiles_after],
    settings_to_review: [...result.settings_to_review],
    steps: result.steps.map(({ id, title, description, kind }) => ({
      id,
      title,
      description,
      kind,
    })),
    can_apply: false,
    requires_operator: true,
    notice: result.notice,
  };
}
