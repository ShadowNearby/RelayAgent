/**
 * DeviceBridge — ArkTS facade the runtime calls, counterpart of
 * android/.../DeviceBridge.kt. One static method per device primitive; the
 * runtime's OnDeviceHarmonyBackend (relay/device.ets) maps onto these.
 *
 * Unlike the Android port (Python over Chaquopy jclass), here the runtime is
 * ArkTS too, so this is a plain same-language facade. Gestures/dump/keys/text
 * forward to the AccessibilityExtension; screencap to ScreenCapture; ask_user
 * to OverlayController.
 */
import image from '@ohos.multimedia.image';
import common from '@ohos.app.ability.common';
import Want from '@ohos.app.ability.Want';
import { UINode } from '../relay/device';
import { RelayAccessibilityExtension } from './RelayAccessibilityExtension';
import { ScreenCapture } from './ScreenCapture';

let stopRequested: boolean = false;

export class DeviceBridge {
  // -- observation --
  static async screencap(): Promise<image.PixelMap | null> {
    return await ScreenCapture.capture();
  }

  static async screenSize(): Promise<[number, number]> {
    return ScreenCapture.screenSize();
  }

  static async dumpUiTree(): Promise<UINode[] | null> {
    const ext = RelayAccessibilityExtension.instance;
    return ext !== null ? await ext.dumpUiTree() : null;
  }

  static async foregroundApp(): Promise<string | null> {
    const ext = RelayAccessibilityExtension.instance;
    return ext !== null ? await ext.foregroundApp() : null;
  }

  // -- gestures / keys --
  static async tap(x: number, y: number): Promise<boolean> {
    const ext = RelayAccessibilityExtension.instance;
    return ext !== null ? await ext.tap(x, y) : false;
  }

  static async longPress(x: number, y: number, durationMs: number): Promise<boolean> {
    const ext = RelayAccessibilityExtension.instance;
    return ext !== null ? await ext.longPress(x, y, durationMs) : false;
  }

  static async swipe(x0: number, y0: number, x1: number, y1: number, durationMs: number): Promise<boolean> {
    const ext = RelayAccessibilityExtension.instance;
    return ext !== null ? await ext.swipe(x0, y0, x1, y1, durationMs) : false;
  }

  static async keyevent(key: string): Promise<boolean> {
    const ext = RelayAccessibilityExtension.instance;
    return ext !== null ? await ext.keyevent(key) : false;
  }

  static async inputText(text: string): Promise<boolean> {
    const ext = RelayAccessibilityExtension.instance;
    return ext !== null ? await ext.inputText(text) : false;
  }

  // -- app lifecycle --
  static async launchApp(appId: string): Promise<boolean> {
    return await AppLauncher.launch(appId);
  }

  static async forceStopApprox(appId: string): Promise<boolean> {
    // No real force-stop without shell privilege (known drift, see android
    // DeviceBridge.kt). A CLEAR_TASK-style relaunch happens on the next launch.
    return false;
  }

  // -- stop flag --
  static requestStop(): void {
    stopRequested = true;
  }
  static resetStop(): void {
    stopRequested = false;
  }
  static shouldStop(): boolean {
    return stopRequested;
  }
}

/**
 * App launch via the ability manager. Bringing a target app to the foreground
 * from the background needs the overlay/background-launch exemption (mirrors
 * android SYSTEM_ALERT_WINDOW). TODO(verify-on-device): bundle->ability
 * resolution (EntryAbility default) and the background-launch exemption.
 */
export class AppLauncher {
  private static appContext: common.UIAbilityContext | null = null;

  static setContext(ctx: common.UIAbilityContext): void {
    AppLauncher.appContext = ctx;
  }

  static async launch(appId: string): Promise<boolean> {
    const ctx = AppLauncher.appContext;
    if (ctx === null) {
      return false;
    }
    const slash = appId.indexOf('/');
    const bundle = slash >= 0 ? appId.substring(0, slash) : appId;
    const ability = slash >= 0 ? appId.substring(slash + 1) : 'EntryAbility';
    const want: Want = { bundleName: bundle, abilityName: ability };
    try {
      await ctx.startAbility(want);
      return true;
    } catch (e) {
      return false;
    }
  }
}
