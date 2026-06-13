/**
 * NativeEnv + runTask — ArkTS port of agents/native_runtime.py.
 *
 * NativeEnv maps a JSONAction onto concrete backend gestures; the
 * geometry-from-direction math lives HERE (swipe distance, scroll reversal),
 * not in the backend — matching the host split. runTask is the in-process
 * obs -> predict -> execute loop with the same terminal-type set and the same
 * ask_user / answer semantics.
 *
 * Divergence from host (idiomatic, not behavioral): the loop is async because
 * HarmonyOS device APIs are Promise-based.
 */
import image from '@ohos.multimedia.image';
import { DeviceBackend, Key } from './device';
import { JSONAction, ActionType } from './actionModel';
import { getInteraction } from './interaction';

export class Observation {
  screenshot: image.PixelMap | null;
  askUserResponse: string | null;

  constructor(screenshot: image.PixelMap | null, askUserResponse: string | null = null) {
    this.screenshot = screenshot;
    this.askUserResponse = askUserResponse;
  }
}

export interface Agent {
  initialize(goal: string): Promise<void>;
  predict(observation: Observation): Promise<[string, JSONAction]>;
  done(): Promise<void>;
  getTotalTokenUsage(): Record<string, number>;
}

export class NativeEnv {
  private backend: DeviceBackend;
  private stepWaitMs: number;
  private waitMs: number;
  private lastScreenshot: image.PixelMap | null = null;

  constructor(backend: DeviceBackend, stepWaitMs: number = 500, waitMs: number = 200) {
    this.backend = backend;
    this.stepWaitMs = stepWaitMs;
    this.waitMs = waitMs;
  }

  async getScreenshot(): Promise<image.PixelMap | null> {
    for (let i = 0; i < 3; i++) {
      const shot = await this.backend.screencap();
      if (shot !== null) {
        this.lastScreenshot = shot;
        return shot;
      }
    }
    return this.lastScreenshot;
  }

  async getObservation(): Promise<Observation> {
    return new Observation(await this.getScreenshot());
  }

  async executeAction(action: JSONAction): Promise<Observation> {
    await this.dispatch(action);
    const sj = action.action_json;
    const skip = sj !== null && (sj['skip_screenshot'] as boolean) === true;
    let shot: image.PixelMap | null;
    if (skip && action.action_type !== ActionType.ASK_USER && this.lastScreenshot !== null) {
      shot = this.lastScreenshot;
    } else {
      shot = await this.getScreenshot();
    }
    return new Observation(shot);
  }

  private async dispatch(action: JSONAction): Promise<void> {
    const at = action.action_type;
    switch (at) {
      case ActionType.CLICK:
        await this.backend.tap(action.x ?? 0, action.y ?? 0);
        break;
      case ActionType.SWIPE:
        await this.swipe(action.x, action.y, action.direction ?? 'up');
        break;
      case ActionType.SCROLL:
        // Scroll direction is content-movement; the gesture is the inverse:
        // scroll up => swipe down, scroll down => swipe up (left/right unchanged).
        await this.swipe(null, null, this.reverse(action.direction ?? 'up'));
        break;
      case ActionType.INPUT_TEXT:
        await this.backend.inputText(action.text ?? '');
        break;
      case ActionType.NAVIGATE_BACK:
        await this.backend.key(Key.BACK);
        break;
      case ActionType.NAVIGATE_HOME:
        await this.backend.key(Key.HOME);
        break;
      case ActionType.KEYBOARD_ENTER:
        await this.backend.key(Key.ENTER);
        break;
      case ActionType.LONG_PRESS:
        await this.backend.longPress(action.x ?? 0, action.y ?? 0);
        break;
      case ActionType.DOUBLE_TAP:
        await this.backend.tap(action.x ?? 0, action.y ?? 0);
        await this.backend.tap(action.x ?? 0, action.y ?? 0);
        break;
      case ActionType.DRAG:
        await this.backend.swipeGesture(
          action.start_x ?? 0, action.start_y ?? 0, action.end_x ?? 0, action.end_y ?? 0);
        break;
      case ActionType.OPEN_APP:
        if (action.app_name !== null && action.app_name.includes('.')) {
          await this.backend.coldLaunch(action.app_name);
        }
        break;
      case ActionType.WAIT:
        await this.sleep(this.waitMs);
        break;
      case ActionType.ANSWER:
      case ActionType.STATUS:
        break;
      default:
        // unhandled action_type — no-op, matching the host's warn-and-skip
        break;
    }
  }

  private reverse(direction: string): string {
    if (direction === 'up') {
      return 'down';
    }
    if (direction === 'down') {
      return 'up';
    }
    return direction;
  }

  private async swipe(x: number | null, y: number | null, direction: string): Promise<void> {
    const size = await this.backend.screenSize();
    const w = size[0];
    const h = size[1];
    const cx = x !== null ? x : Math.floor(w / 2);
    const cy = y !== null ? y : Math.floor(h / 2);
    const unit = (w / 10) * 2;
    let dx = 0;
    let dy = 0;
    if (direction === 'up') {
      dy = -2 * unit;
    } else if (direction === 'down') {
      dy = 2 * unit;
    } else if (direction === 'left') {
      dx = -unit;
    } else if (direction === 'right') {
      dx = unit;
    }
    await this.backend.swipeGesture(cx, cy, cx + dx, cy + dy);
  }

  sleep(ms: number): Promise<void> {
    return new Promise<void>((resolve) => setTimeout(resolve, ms));
  }

  stepWait(): Promise<void> {
    return this.sleep(this.stepWaitMs);
  }
}

const TERMINAL_TYPES: Set<string> = new Set<string>([
  ActionType.FINISHED, ActionType.UNKNOWN, ActionType.ENV_FAIL,
]);

export interface RunSummary {
  steps: number;
  last_action_type: string | null;
  last_goal_status: string | null;
  token_usage: Record<string, number>;
}

/** In-process obs -> predict -> execute loop (agents/native_runtime.run_task). */
export async function runTask(
  goal: string, agent: Agent, env: NativeEnv, maxStep: number = -1,
): Promise<RunSummary> {
  const interaction = getInteraction();
  await agent.initialize(goal);
  let obs = await env.getObservation();
  let step = 0;
  let lastActionType: string | null = null;
  let lastGoalStatus: string | null = null;

  while (!interaction.shouldStop()) {
    step += 1;
    const pred = await agent.predict(obs);
    const thought = pred[0];
    const action = pred[1];
    lastActionType = action.action_type;
    lastGoalStatus = action.goal_status;
    interaction.emitStatus({
      'event': 'step', 'step': step,
      'action_type': lastActionType ?? '', 'thought': thought,
    });

    if (lastActionType !== null && TERMINAL_TYPES.has(lastActionType)) {
      break;
    }
    if (lastActionType === ActionType.ASK_USER) {
      const answer = await interaction.askUser(action.text ?? 'The agent needs your input');
      if (answer === null) {
        break; // EOF / take-over = success terminal
      }
      const shot = await env.getScreenshot();
      obs = new Observation(shot, answer);
      continue;
    }
    if (lastActionType === ActionType.ANSWER) {
      await env.executeAction(action);
      break;
    }
    obs = await env.executeAction(action);
    await env.stepWait();
    if (maxStep > 0 && step >= maxStep) {
      break;
    }
  }

  await agent.done();
  return {
    steps: step,
    last_action_type: lastActionType,
    last_goal_status: lastGoalStatus,
    token_usage: agent.getTotalTokenUsage(),
  };
}
