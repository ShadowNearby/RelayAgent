/**
 * SettingsStore — counterpart of android/.../SettingsActivity.kt.
 *
 * The on-device replacement for the host .env: LLM_BASE_URL / LLM_API_KEY /
 * LLM_MODEL, persisted via @ohos.data.preferences. (Encrypted preferences /
 * the asset store can replace this for the API key; preferences keeps the
 * Phase-1 surface simple.)
 */
import preferences from '@ohos.data.preferences';
import common from '@ohos.app.ability.common';
import { LlmConfig } from '../relay/nativeRunner';

const STORE = 'relay_settings';
const KEYS = ['LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL'];

export class SettingsStore {
  private static async store(ctx: common.UIAbilityContext): Promise<preferences.Preferences> {
    return await preferences.getPreferences(ctx, STORE);
  }

  static async load(ctx: common.UIAbilityContext): Promise<LlmConfig> {
    const p = await SettingsStore.store(ctx);
    return {
      baseUrl: String(await p.get('LLM_BASE_URL', '')),
      apiKey: String(await p.get('LLM_API_KEY', '')),
      model: String(await p.get('LLM_MODEL', 'qwen')),
    };
  }

  static async save(ctx: common.UIAbilityContext, cfg: LlmConfig): Promise<void> {
    const p = await SettingsStore.store(ctx);
    await p.put('LLM_BASE_URL', cfg.baseUrl);
    await p.put('LLM_API_KEY', cfg.apiKey);
    await p.put('LLM_MODEL', cfg.model);
    await p.flush();
  }

  static isConfigured(cfg: LlmConfig): boolean {
    return cfg.baseUrl.length > 0;
  }
}
