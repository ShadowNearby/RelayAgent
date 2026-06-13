/**
 * ScreenCapture — counterpart of android/.../ScreenCaptureService.kt.
 *
 * Phase 1 scope: screenSize only (from @ohos.display). Frame capture
 * (AVScreenCapture) is a Phase 2 item — the minimal single-app loop drives off
 * the a11y tree, not screenshot pixels, so capture() returns null for now and
 * the runtime degrades gracefully (no VLM grounding in Phase 1). Wiring
 * AVScreenCapture here is what unlocks the VLM fallback paths later.
 */
import image from '@ohos.multimedia.image';
import display from '@ohos.display';

export class ScreenCapture {
  static async capture(): Promise<image.PixelMap | null> {
    // TODO(Phase 2): AVScreenCapture -> Surface -> PixelMap, gated on the
    // user's screen-capture consent (analogous to MediaProjection).
    return null;
  }

  static screenSize(): [number, number] {
    try {
      const d = display.getDefaultDisplaySync();
      return [d.width, d.height];
    } catch (_e) {
      return [1080, 2400];
    }
  }
}
