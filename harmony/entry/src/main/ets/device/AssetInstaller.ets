/**
 * AssetInstaller — counterpart of android/.../AssetInstaller.kt.
 *
 * Loads the bundled manifest cards (synced from the repo at build time by
 * scripts/sync_harmony_assets.py into resources/rawfile/relay/manifests/*.json)
 * and exposes a card by app_id. Unlike the Android port these are read straight
 * from rawfile (no copy to filesDir needed — the ArkTS runtime reads the
 * resource manager directly).
 */
import common from '@ohos.app.ability.common';
import { util } from '@kit.ArkTS';
import { Card } from '../relay/agent';

export class AssetInstaller {
  /** Load and parse a manifest card JSON for the given app_id. */
  static async loadCard(ctx: common.UIAbilityContext, appId: string): Promise<Card | null> {
    try {
      const path = `relay/manifests/${appId}.json`;
      const bytes = await ctx.resourceManager.getRawFileContent(path);
      const text = util.TextDecoder.create('utf-8').decodeWithStream(bytes);
      return JSON.parse(text) as Card;
    } catch (_e) {
      return null;
    }
  }
}
