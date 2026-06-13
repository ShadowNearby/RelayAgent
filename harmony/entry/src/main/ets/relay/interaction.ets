/**
 * InteractionProvider — ArkTS port of agents/interaction.py.
 *
 * ask_user returns null on EOF/take-over, which the loop treats as a SUCCESS
 * terminal (the same semantics as the host's redirected-stdin EOF). The
 * Android port backs this with the overlay panel; here OverlayInteraction maps
 * onto OverlayController (回答 -> answer, 我来接管 -> null).
 */
import { OverlayController } from '../device/OverlayController';

export interface InteractionProvider {
  /** Show text, block for the user's answer; null = EOF / take-over (terminal). */
  askUser(text: string | null): Promise<string | null>;
  /** Fire-and-forget telemetry; never throws. */
  emitStatus(event: Record<string, Object>): void;
  /** Polled at loop boundaries; true => stop the run. */
  shouldStop(): boolean;
}

/** Default: no UI. Mirrors TerminalInteraction's non-interactive behavior. */
export class NullInteraction implements InteractionProvider {
  async askUser(_text: string | null): Promise<string | null> {
    return null;
  }
  emitStatus(_event: Record<string, Object>): void {}
  shouldStop(): boolean {
    return false;
  }
}

export class OverlayInteraction implements InteractionProvider {
  async askUser(text: string | null): Promise<string | null> {
    return await OverlayController.askUserBlocking(text ?? '');
  }
  emitStatus(event: Record<string, Object>): void {
    try {
      OverlayController.postStatus(JSON.stringify(event));
    } catch (_e) {
      // status is fire-and-forget; never break the loop
    }
  }
  shouldStop(): boolean {
    return OverlayController.shouldStop();
  }
}

let _interaction: InteractionProvider = new NullInteraction();

export function setInteraction(provider: InteractionProvider): void {
  _interaction = provider;
}

export function getInteraction(): InteractionProvider {
  return _interaction;
}
