/**
 * DeviceBackend seam — the ArkTS counterpart of agents/device/base.py.
 *
 * The host runtime is Python over a synchronous DeviceBackend ABC; on
 * HarmonyOS the platform device APIs (accessibility, screen capture) are
 * Promise-based, so this interface is async. `OnDeviceHarmonyBackend` maps it
 * onto the native `DeviceBridge` (accessibility extension + AVScreenCapture),
 * the analogue of relay_android/backend.py over the Kotlin DeviceBridge.
 *
 * Contract parity with the Python UINode (agents/device/base.py):
 *   bounds = [x1, y1, x2, y2] in screen pixels; center is null when absent or
 *   zero/negative-area (consumers use that as a visibility filter).
 */
import image from '@ohos.multimedia.image';
import { DeviceBridge } from '../device/DeviceBridge';

export enum Key {
  BACK = 'back',
  HOME = 'home',
  ENTER = 'enter',
}

export class UINode {
  text: string = '';
  desc: string = '';
  resourceId: string = '';
  className: string = '';
  pkg: string = '';
  bounds: [number, number, number, number] | null = null;
  clickable: boolean = false;
  longClickable: boolean = false;
  scrollable: boolean = false;
  focusable: boolean = false;
  enabled: boolean = true;

  /** Center of bounds; null when absent or zero/negative area. */
  center(): [number, number] | null {
    const b = this.bounds;
    if (b === null) {
      return null;
    }
    const x1 = b[0];
    const y1 = b[1];
    const x2 = b[2];
    const y2 = b[3];
    if (x2 <= x1 || y2 <= y1) {
      return null;
    }
    return [Math.floor((x1 + x2) / 2), Math.floor((y1 + y2) / 2)];
  }
}

export interface DeviceBackend {
  // -- observation --
  screencap(): Promise<image.PixelMap | null>;
  screenSize(): Promise<[number, number]>;
  dumpUiTree(): Promise<UINode[] | null>;
  foregroundApp(): Promise<string | null>;
  // -- gestures / input --
  tap(x: number, y: number): Promise<boolean>;
  longPress(x: number, y: number, durationMs?: number): Promise<void>;
  swipeGesture(x0: number, y0: number, x1: number, y1: number, durationMs?: number): Promise<void>;
  key(key: Key): Promise<void>;
  inputText(text: string): Promise<void>;
  // -- app lifecycle --
  launch(appId: string): Promise<void>;
  coldLaunch(appId: string, settleSeconds?: number): Promise<void>;
  forceStop(appId: string): Promise<void>;
}

export function sleep(ms: number): Promise<void> {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

/**
 * DeviceBackend over the native DeviceBridge. Geometry-from-direction math
 * (swipe distance, scroll reversal) stays in NativeEnv — this layer only
 * forwards concrete gestures, exactly like the Python backends.
 */
export class OnDeviceHarmonyBackend implements DeviceBackend {
  private screenSizeCache: [number, number] | null = null;

  async screencap(): Promise<image.PixelMap | null> {
    return await DeviceBridge.screencap();
  }

  async screenSize(): Promise<[number, number]> {
    if (this.screenSizeCache === null) {
      this.screenSizeCache = await DeviceBridge.screenSize();
    }
    return this.screenSizeCache;
  }

  async dumpUiTree(): Promise<UINode[] | null> {
    return await DeviceBridge.dumpUiTree();
  }

  async foregroundApp(): Promise<string | null> {
    return await DeviceBridge.foregroundApp();
  }

  async tap(x: number, y: number): Promise<boolean> {
    return await DeviceBridge.tap(Math.round(x), Math.round(y));
  }

  async longPress(x: number, y: number, durationMs: number = 1000): Promise<void> {
    await DeviceBridge.longPress(Math.round(x), Math.round(y), durationMs);
  }

  async swipeGesture(
    x0: number, y0: number, x1: number, y1: number, durationMs: number = 400,
  ): Promise<void> {
    await DeviceBridge.swipe(Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1), durationMs);
  }

  async key(key: Key): Promise<void> {
    await DeviceBridge.keyevent(key);
  }

  async inputText(text: string): Promise<void> {
    await DeviceBridge.inputText(text);
  }

  async launch(appId: string): Promise<void> {
    const ok = await DeviceBridge.launchApp(appId);
    if (!ok) {
      throw new Error(`Failed to launch ${appId}`);
    }
  }

  /**
   * Cold launch. NOTE: like relay_android, there is no real force-stop without
   * shell privilege — a CLEAR_TASK-style relaunch is the closest approximation,
   * so target-app in-memory state may survive across legs (known drift).
   */
  async coldLaunch(appId: string, settleSeconds: number = 1.0): Promise<void> {
    await this.forceStop(appId);
    await this.launch(appId);
    await sleep(settleSeconds * 1000);
  }

  async forceStop(appId: string): Promise<void> {
    await DeviceBridge.forceStopApprox(appId);
  }
}
