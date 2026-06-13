/**
 * RelayAccessibilityExtension — the device-driving AccessibilityExtensionAbility,
 * counterpart of android/.../RelayAccessibilityService.kt.
 *
 * Provides: gesture injection (tap/longPress/swipe via injectGesture), the
 * normalized a11y tree (getWindowRootElement traversal -> UINode[], so the
 * runtime never touches the platform format), foreground bundle, and
 * focused-field text input.
 *
 * Uses the typed @kit.AccessibilityKit API (attributeValue is keyed by
 * ElementAttributeValues). TODO(verify-on-device): the global back/home action
 * and the exact setText parameter shape still need a real-device check.
 */
import { AccessibilityExtensionAbility, AccessibilityElement, AccessibilityEvent, GesturePath, GesturePoint, Rect }
  from '@kit.AccessibilityKit';
import { UINode } from '../relay/device';

export class RelayAccessibilityExtension extends AccessibilityExtensionAbility {
  static instance: RelayAccessibilityExtension | null = null;

  onConnect(): void {
    RelayAccessibilityExtension.instance = this;
  }

  onDisconnect(): void {
    if (RelayAccessibilityExtension.instance === this) {
      RelayAccessibilityExtension.instance = null;
    }
  }

  // Pull model: the runtime polls dumps; no event handling needed.
  onAccessibilityEvent(event: AccessibilityEvent): void {}

  // -- observation ----------------------------------------------------------
  async dumpUiTree(): Promise<UINode[] | null> {
    try {
      const root: AccessibilityElement = await this.context.getWindowRootElement();
      const out: UINode[] = [];
      await this.walk(root, out);
      return out;
    } catch (e) {
      return null;
    }
  }

  private async walk(el: AccessibilityElement, out: UINode[]): Promise<void> {
    const node = new UINode();
    node.text = await this.str(el, 'text');
    node.desc = await this.str(el, 'description');
    node.resourceId = await this.str(el, 'inspectorKey');
    node.className = await this.str(el, 'componentType');
    node.pkg = await this.str(el, 'bundleName');
    node.bounds = await this.rect(el);
    node.clickable = await this.bool(el, 'clickable');
    node.longClickable = await this.bool(el, 'longClickable');
    node.scrollable = await this.bool(el, 'scrollable');
    node.focusable = await this.bool(el, 'focusable');
    node.enabled = await this.bool(el, 'isEnable');
    out.push(node);
    try {
      const children: Array<AccessibilityElement> = await el.attributeValue('children');
      for (const child of children) {
        await this.walk(child, out);
      }
    } catch (e) {
      // leaf or unreadable children
    }
  }

  private async str(el: AccessibilityElement,
    key: 'text' | 'description' | 'inspectorKey' | 'componentType' | 'bundleName'): Promise<string> {
    try {
      const v: string = await el.attributeValue(key);
      return v ? v.trim() : '';
    } catch (e) {
      return '';
    }
  }

  private async bool(el: AccessibilityElement,
    key: 'clickable' | 'longClickable' | 'scrollable' | 'focusable' | 'isEnable'): Promise<boolean> {
    try {
      const v: boolean = await el.attributeValue(key);
      return v === true;
    } catch (e) {
      return false;
    }
  }

  private async rect(el: AccessibilityElement): Promise<[number, number, number, number] | null> {
    try {
      const r: Rect = await el.attributeValue('screenRect');
      return [r.left, r.top, r.left + r.width, r.top + r.height];
    } catch (e) {
      return null;
    }
  }

  async foregroundApp(): Promise<string | null> {
    try {
      const root: AccessibilityElement = await this.context.getWindowRootElement();
      const bundle: string = await root.attributeValue('bundleName');
      return bundle ? bundle : null;
    } catch (e) {
      return null;
    }
  }

  // -- gestures -------------------------------------------------------------
  private async dispatch(points: Array<GesturePoint>, durationMs: number): Promise<boolean> {
    try {
      const path: GesturePath = new GesturePath(durationMs);
      path.points = points;
      await this.context.injectGesture(path);
      return true;
    } catch (e) {
      return false;
    }
  }

  async tap(x: number, y: number): Promise<boolean> {
    return await this.dispatch([new GesturePoint(x, y)], 50);
  }

  async longPress(x: number, y: number, durationMs: number): Promise<boolean> {
    return await this.dispatch([new GesturePoint(x, y)], durationMs);
  }

  async swipe(x0: number, y0: number, x1: number, y1: number, durationMs: number): Promise<boolean> {
    return await this.dispatch([new GesturePoint(x0, y0), new GesturePoint(x1, y1)], durationMs);
  }

  // -- keys -----------------------------------------------------------------
  async keyevent(key: string): Promise<boolean> {
    // TODO(verify-on-device): AccessibilityExtensionContext has no global
    // back/home in this SDK; ENTER would need an IME action on the focused
    // field. Returns false for now (submit can be a Send-button tap instead).
    return false;
  }

  // -- text input -----------------------------------------------------------
  async inputText(text: string): Promise<boolean> {
    try {
      // getFocusElement(false) = the input-focused element (not the a11y focus).
      const focused: AccessibilityElement = await this.context.getFocusElement(false);
      const params: Record<string, string> = { 'text': text };
      await focused.performAction('setText', params);
      return true;
    } catch (e) {
      return false;
    }
  }
}
