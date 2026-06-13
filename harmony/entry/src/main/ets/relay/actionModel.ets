/**
 * JSONAction — ArkTS port of agents/action_model.py (pure Python, pinned by
 * tests/test_action_model.py). Field order, validators, modelDump and equality
 * are kept faithful so the two runtimes never drift; see harmony/README.md.
 *
 * ArkTS note: no dynamic string-indexing of class members, so modelDump /
 * equals enumerate fields explicitly (the put() helper) instead of reflecting.
 */

export class ActionType {
  static readonly CLICK = 'click';
  static readonly DOUBLE_TAP = 'double_tap';
  static readonly SCROLL = 'scroll';
  static readonly SWIPE = 'swipe';
  static readonly INPUT_TEXT = 'input_text';
  static readonly NAVIGATE_HOME = 'navigate_home';
  static readonly NAVIGATE_BACK = 'navigate_back';
  static readonly KEYBOARD_ENTER = 'keyboard_enter';
  static readonly OPEN_APP = 'open_app';
  static readonly STATUS = 'status';
  static readonly WAIT = 'wait';
  static readonly LONG_PRESS = 'long_press';
  static readonly ANSWER = 'answer';
  static readonly FINISHED = 'finished';
  static readonly UNKNOWN = 'unknown';
  static readonly DRAG = 'drag';
  static readonly ASK_USER = 'ask_user';
  static readonly MCP = 'mcp';
  static readonly ENV_FAIL = 'error_env';
}

const ACTION_TYPES: Set<string> = new Set<string>([
  ActionType.CLICK, ActionType.DOUBLE_TAP, ActionType.SCROLL, ActionType.SWIPE,
  ActionType.INPUT_TEXT, ActionType.NAVIGATE_HOME, ActionType.NAVIGATE_BACK,
  ActionType.KEYBOARD_ENTER, ActionType.OPEN_APP, ActionType.STATUS, ActionType.WAIT,
  ActionType.LONG_PRESS, ActionType.ANSWER, ActionType.FINISHED, ActionType.UNKNOWN,
  ActionType.DRAG, ActionType.ASK_USER, ActionType.MCP, ActionType.ENV_FAIL,
]);

const SCROLL_DIRECTIONS: Set<string> = new Set<string>(['left', 'right', 'down', 'up']);

export interface JSONActionInit {
  action_type?: string | null;
  index?: number | null;
  x?: number | null;
  y?: number | null;
  text?: string | null;
  direction?: string | null;
  goal_status?: string | null;
  app_name?: string | null;
  keycode?: string | null;
  start_x?: number | null;
  start_y?: number | null;
  end_x?: number | null;
  end_y?: number | null;
  action_name?: string | null;
  action_json?: Record<string, Object> | null;
}

export class JSONAction {
  action_type: string | null = null;
  index: number | null = null;
  x: number | null = null;
  y: number | null = null;
  text: string | null = null;
  direction: string | null = null;
  goal_status: string | null = null;
  app_name: string | null = null;
  keycode: string | null = null;
  start_x: number | null = null;
  start_y: number | null = null;
  end_x: number | null = null;
  end_y: number | null = null;
  action_name: string | null = null;
  action_json: Record<string, Object> | null = null;

  constructor(init: JSONActionInit = {}) {
    if (init.action_type !== undefined && init.action_type !== null) {
      if (!ACTION_TYPES.has(init.action_type)) {
        throw new Error(`Invalid action_type: ${init.action_type}`);
      }
      this.action_type = init.action_type;
    }
    if (init.direction !== undefined && init.direction !== null) {
      if (!SCROLL_DIRECTIONS.has(init.direction)) {
        throw new Error(`Invalid direction: ${init.direction}`);
      }
      this.direction = init.direction;
    }
    if (init.keycode !== undefined && init.keycode !== null) {
      if (!init.keycode.startsWith('KEYCODE_')) {
        throw new Error(`keycode must start with KEYCODE_: ${init.keycode}`);
      }
      this.keycode = init.keycode;
    }
    if (init.index !== undefined && init.index !== null) {
      this.index = Math.round(init.index);
    }
    if (init.x !== undefined && init.x !== null) {
      this.x = Math.round(init.x);
    }
    if (init.y !== undefined && init.y !== null) {
      this.y = Math.round(init.y);
    }
    // Mutual exclusion (mirrors the Python validator).
    if (this.index !== null && (this.x !== null || this.y !== null)) {
      throw new Error('Either an index or a <x, y> should be provided, not both');
    }
    if (init.text !== undefined && init.text !== null) {
      this.text = String(init.text);
    }
    this.goal_status = init.goal_status !== undefined ? init.goal_status : null;
    this.app_name = init.app_name !== undefined ? init.app_name : null;
    this.start_x = init.start_x !== undefined ? init.start_x : null;
    this.start_y = init.start_y !== undefined ? init.start_y : null;
    this.end_x = init.end_x !== undefined ? init.end_x : null;
    this.end_y = init.end_y !== undefined ? init.end_y : null;
    this.action_name = init.action_name !== undefined ? init.action_name : null;
    this.action_json = init.action_json !== undefined ? init.action_json : null;
  }

  private put(
    out: Record<string, Object | null>, name: string, value: Object | null, excludeNone: boolean,
  ): void {
    if (excludeNone && value === null) {
      return;
    }
    out[name] = value;
  }

  /** Fields in declaration order (== Python model_dump order). */
  modelDump(excludeNone: boolean = false): Record<string, Object | null> {
    const out: Record<string, Object | null> = {};
    this.put(out, 'action_type', this.action_type, excludeNone);
    this.put(out, 'index', this.index, excludeNone);
    this.put(out, 'x', this.x, excludeNone);
    this.put(out, 'y', this.y, excludeNone);
    this.put(out, 'text', this.text, excludeNone);
    this.put(out, 'direction', this.direction, excludeNone);
    this.put(out, 'goal_status', this.goal_status, excludeNone);
    this.put(out, 'app_name', this.app_name, excludeNone);
    this.put(out, 'keycode', this.keycode, excludeNone);
    this.put(out, 'start_x', this.start_x, excludeNone);
    this.put(out, 'start_y', this.start_y, excludeNone);
    this.put(out, 'end_x', this.end_x, excludeNone);
    this.put(out, 'end_y', this.end_y, excludeNone);
    this.put(out, 'action_name', this.action_name, excludeNone);
    this.put(out, 'action_json', this.action_json, excludeNone);
    return out;
  }

  private normalized(): Record<string, Object | null> {
    const d = this.modelDump(false);
    const t = d['text'];
    if (typeof t === 'string') {
      d['text'] = t.toLowerCase();
    }
    const a = d['app_name'];
    if (typeof a === 'string') {
      d['app_name'] = a.toLowerCase();
    }
    return d;
  }

  /** Case-insensitive for app_name/text, exact otherwise (mirrors _compare_actions). */
  equals(other: JSONAction): boolean {
    return JSON.stringify(this.normalized()) === JSON.stringify(other.normalized());
  }
}
