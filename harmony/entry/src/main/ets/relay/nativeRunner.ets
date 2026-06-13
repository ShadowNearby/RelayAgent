/**
 * runLeg — ArkTS port of agents/native_runner.run_leg (single-app entry).
 * Assembles backend + NativeEnv + RelayAgent and runs the obs->predict->execute
 * loop. The NL cross-app flow layer (flow_runner / nl_flow) is Phase 3.
 */
import { OnDeviceHarmonyBackend } from './device';
import { NativeEnv, runTask, RunSummary } from './nativeEnv';
import { RelayAgent, Card } from './agent';
import { LlmClient } from './llmClient';

export interface LlmConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
}

export async function runLeg(
  card: Card, goal: string, cfg: LlmConfig, maxStep: number = -1,
): Promise<RunSummary> {
  const backend = new OnDeviceHarmonyBackend();
  const env = new NativeEnv(backend);
  const llm = new LlmClient(cfg.baseUrl, cfg.apiKey);
  const agent = new RelayAgent(card, llm, cfg.model, backend);
  return await runTask(goal, agent, env, maxStep);
}
