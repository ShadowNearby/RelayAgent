/**
 * EntryAbility — app entry UIAbility (counterpart of the Android Application +
 * MainActivity bootstrap). Loads the Index page and hands the AppLauncher its
 * context so the runtime can bring target apps to the foreground.
 */
import UIAbility from '@ohos.app.ability.UIAbility';
import window from '@ohos.window';
import { AppLauncher } from '../device/DeviceBridge';

export default class EntryAbility extends UIAbility {
  onWindowStageCreate(windowStage: window.WindowStage): void {
    AppLauncher.setContext(this.context);
    windowStage.loadContent('pages/Index');
  }
}
