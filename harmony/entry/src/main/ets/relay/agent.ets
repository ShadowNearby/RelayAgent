/**
 * RelayAgent (minimal) — ArkTS port of the Phase-1 subset of
 * agents/relay_agent.py + action_planner.py + capability_router.py.
 *
 * Supports the deterministic + a11y-only step kinds only:
 *   open_app / tap_text (a11y tree match, NO VLM grounding) / input_text /
 *   wait_for_reply (text-hash stability, NO VLM done-judge) / answer.
 *
 * Deferred to Phase 2/3 (see harmony/README.md): VLM grounding fallback for
 * tap_text, VLM reply read-back, multi-screen scroll capture, and the whole NL
 * cross-app flow layer (flow_planner / three-stage matrix router / nl_flow).
 */
import image from '@ohos.multimedia.image';
import { NativeEnv, Observation, Agent } from './nativeEnv';
import { JSONAction, ActionType } from './actionModel';
import { DeviceBackend, UINode, sleep } from './device';
import { LlmClient, ChatMessage } from './llmClient';

// -- card model (subset of a manifest, produced by sync_harmony_assets.py) ----
export interface Capability {
  id: string;
  description: string;
  input_hint?: string;     // text/desc of the message box to focus
}

export interface Card {
  app_id: string;
  app_name: string;
  agent_name?: string;
  capabilities: Capability[];
}

// -- plan steps (subset of action_planner.Step) -------------------------------
interface Step {
  kind: string;            // open_app | tap_text | input_text | wait_for_reply | answer
  payload: Record<string, string>;
}

const STABLE_DUMPS_FOR_DONE: number = 3;
const POLL_INTERVAL_MS: number = 1200;
const REPLY_CHROME: Set<string> = new Set<string>(['发送', 'Send', '语音', '相机', '更多']);

export class RelayAgent implements Agent {
  private card: Card;
  private llm: LlmClient;
  private model: string;
  private backend: DeviceBackend;
  private goal: string = '';
  private plan: Step[] = [];
  private cursor: number = 0;
  private replyText: string = '';
  private tokenUsage: Record<string, number> = {
    'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
  };

  constructor(card: Card, llm: LlmClient, model: string, backend: DeviceBackend) {
    this.card = card;
    this.llm = llm;
    this.model = model;
    this.backend = backend;
  }

  async initialize(goal: string): Promise<void> {
    this.goal = goal;
  }

  getTotalTokenUsage(): Record<string, number> {
    return this.tokenUsage;
  }

  async done(): Promise<void> {}

  async predict(_observation: Observation): Promise<[string, JSONAction]> {
    // First call: route to a capability (1 LLM call) and build the plan.
    if (this.plan.length === 0) {
      const route = await this.routeCapability();
      this.plan = this.buildPlan(route[0], route[1]);
      this.cursor = 0;
    }
    if (this.cursor >= this.plan.length) {
      return ['[done] plan exhausted', new JSONAction({ action_type: ActionType.FINISHED })];
    }
    const step = this.plan[this.cursor];
    return await this.materialize(step);
  }

  // -- routing ----------------------------------------------------------------
  private async routeCapability(): Promise<[string, string]> {
    const caps = this.card.capabilities
      .map((c) => `- ${c.id}: ${c.description}`).join('\n');
    const messages: ChatMessage[] = [
      {
        role: 'system',
        content: 'You pick the single best capability for the user request and '
          + 'write the exact instruction to send to the in-app assistant. '
          + 'Reply with JSON: {"capability_id": "...", "invocation_text": "..."}.',
      },
      {
        role: 'user',
        content: `App: ${this.card.app_name}\nCapabilities:\n${caps}\n\nRequest: ${this.goal}`,
      },
    ];
    const resp = await this.llm.create(this.model, messages, { 'temperature': 0 });
    this.accountTokens(resp.usage.prompt_tokens, resp.usage.completion_tokens, resp.usage.total_tokens);
    let capId = this.card.capabilities.length > 0 ? this.card.capabilities[0].id : 'chat';
    let invocation = this.goal;
    try {
      const parsed = JSON.parse(this.extractJson(resp.content)) as Record<string, string>;
      if (parsed['capability_id']) {
        capId = parsed['capability_id'];
      }
      if (parsed['invocation_text']) {
        invocation = parsed['invocation_text'];
      }
    } catch (_e) {
      // fall back to first capability + raw goal (logged loud in host; minimal here)
    }
    return [capId, invocation];
  }

  private extractJson(s: string): string {
    const start = s.indexOf('{');
    const end = s.lastIndexOf('}');
    return start >= 0 && end > start ? s.slice(start, end + 1) : s;
  }

  // -- planning (action_planner.build_plan, chat-flow subset) ------------------
  private buildPlan(capId: string, invocation: string): Step[] {
    const cap = this.card.capabilities.find((c) => c.id === capId);
    const hint = (cap !== undefined && cap.input_hint !== undefined) ? cap.input_hint : '';
    return [
      { kind: 'open_app', payload: { 'app': this.card.app_id } },
      { kind: 'tap_text', payload: { 'text': hint } },
      { kind: 'input_text', payload: { 'text': invocation } },
      { kind: 'submit', payload: {} },
      { kind: 'wait_for_reply', payload: {} },
      { kind: 'answer', payload: {} },
    ];
  }

  // -- step materialization ---------------------------------------------------
  private async materialize(step: Step): Promise<[string, JSONAction]> {
    switch (step.kind) {
      case 'open_app':
        this.cursor += 1;
        return [`open ${step.payload['app']}`,
          new JSONAction({ action_type: ActionType.OPEN_APP, app_name: step.payload['app'] })];
      case 'tap_text': {
        this.cursor += 1;
        const xy = await this.groundText(step.payload['text']);
        if (xy === null) {
          return ['tap_text: target not in a11y tree; skipping',
            new JSONAction({ action_type: ActionType.WAIT })];
        }
        return [`tap "${step.payload['text']}" at ${xy}`,
          new JSONAction({ action_type: ActionType.CLICK, x: xy[0], y: xy[1] })];
      }
      case 'input_text':
        this.cursor += 1;
        return [`type: ${step.payload['text']}`,
          new JSONAction({ action_type: ActionType.INPUT_TEXT, text: step.payload['text'] })];
      case 'submit':
        this.cursor += 1;
        return ['submit (enter)', new JSONAction({ action_type: ActionType.KEYBOARD_ENTER })];
      case 'wait_for_reply': {
        // Block here polling for text-hash stability (NO VLM). Holds the cursor
        // until the reply settles, then advances.
        this.replyText = await this.waitForReply();
        this.cursor += 1;
        return ['[hold] waiting for reply (text-hash stable)',
          new JSONAction({ action_type: ActionType.WAIT })];
      }
      case 'answer':
        this.cursor += 1;
        return [`answer: ${this.replyText.slice(0, 80)}`,
          new JSONAction({ action_type: ActionType.ANSWER, text: this.replyText })];
      default:
        this.cursor += 1;
        return [`unknown step ${step.kind}`,
          new JSONAction({ action_type: ActionType.FINISHED })];
    }
  }

  /** a11y-only grounding: match text/desc in the dump, return center. */
  private async groundText(text: string): Promise<[number, number] | null> {
    if (!text) {
      return null;
    }
    for (let attempt = 0; attempt < 3; attempt++) {
      const nodes = await this.backend.dumpUiTree();
      if (nodes !== null) {
        const hit = nodes.find((n) =>
          (n.text === text || n.desc === text) && n.center() !== null);
        if (hit !== undefined) {
          return hit.center();
        }
      }
      await sleep(800);
    }
    return null;
  }

  /** Poll until the on-screen text is byte-identical for STABLE_DUMPS_FOR_DONE
   * consecutive reads, then scrape the reply. Mirrors the host text-hash done
   * detection (no VLM judge). */
  private async waitForReply(): Promise<string> {
    let stable = 0;
    let lastHash = '';
    let lastNodes: UINode[] = [];
    for (let i = 0; i < 60; i++) {
      await sleep(POLL_INTERVAL_MS);
      const nodes = await this.backend.dumpUiTree();
      if (nodes === null) {
        continue;
      }
      const h = nodes.map((n) => n.text).join('');
      if (h === lastHash) {
        stable += 1;
        if (stable >= STABLE_DUMPS_FOR_DONE) {
          lastNodes = nodes;
          break;
        }
      } else {
        stable = 0;
        lastHash = h;
        lastNodes = nodes;
      }
    }
    return this.scrapeReply(lastNodes);
  }

  /** Pick the longest non-chrome text node as the reply (minimal scrape). */
  private scrapeReply(nodes: UINode[]): string {
    let best = '';
    for (const n of nodes) {
      const t = n.text.trim();
      if (t.length === 0 || REPLY_CHROME.has(t)) {
        continue;
      }
      if (t.length > best.length) {
        best = t;
      }
    }
    return best;
  }

  private accountTokens(p: number, c: number, t: number): void {
    this.tokenUsage['prompt_tokens'] += p;
    this.tokenUsage['completion_tokens'] += c;
    this.tokenUsage['total_tokens'] += t;
  }
}
