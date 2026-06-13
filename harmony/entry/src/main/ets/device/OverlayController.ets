/**
 * OverlayController — counterpart of android/.../OverlayController.kt.
 *
 * A floating system window (SYSTEM_FLOAT_WINDOW) hosting the status chip and
 * the ask_user panel. ask_user blocks the worker until the user answers (回答 ->
 * string) or takes over (我来接管 -> null = handoff terminal, same as host EOF).
 *
 * Phase 1: the plumbing (blocking promise + stop flag + status sink) is wired;
 * the floating-window UI itself is a thin placeholder (TODO: build the panel
 * with @ohos.window createWindow + a loaded ArkUI component).
 */
import { DeviceBridge } from './DeviceBridge';

type Resolver = (answer: string | null) => void;

export class OverlayController {
  private static pendingResolver: Resolver | null = null;
  private static pendingPrompt: string = '';
  private static lastStatus: string = '';
  private static onStatus: ((s: string) => void) | null = null;
  private static onAsk: ((prompt: string) => void) | null = null;

  /** Register UI sinks (the page/overlay component wires these). */
  static bind(onStatus: (s: string) => void, onAsk: (prompt: string) => void): void {
    OverlayController.onStatus = onStatus;
    OverlayController.onAsk = onAsk;
  }

  static show(): void {
    // TODO: window.createWindow(floating) + load the overlay component.
  }
  static hide(): void {
    // TODO: destroy the floating window.
  }

  /** Blocking ask_user: resolves when the user answers or takes over (null). */
  static askUserBlocking(prompt: string): Promise<string | null> {
    OverlayController.pendingPrompt = prompt;
    OverlayController.onAsk?.(prompt);
    return new Promise<string | null>((resolve) => {
      OverlayController.pendingResolver = resolve;
    });
  }

  /** Called by the UI when the user answers (string) or takes over (null). */
  static resolveAsk(answer: string | null): void {
    const r = OverlayController.pendingResolver;
    OverlayController.pendingResolver = null;
    if (r !== null) {
      r(answer);
    }
  }

  static postStatus(json: string): void {
    OverlayController.lastStatus = json;
    OverlayController.onStatus?.(json);
  }
  static currentStatus(): string {
    return OverlayController.lastStatus;
  }

  static shouldStop(): boolean {
    return DeviceBridge.shouldStop();
  }
}
