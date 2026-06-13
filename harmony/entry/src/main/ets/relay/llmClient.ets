/**
 * LlmClient — ArkTS port of the stdlib HTTP shim in agents/llm_client.py
 * (HttpChatClient). One POST to {baseUrl}/chat/completions, no streaming,
 * response normalized to the same surface callers read:
 *   resp.choices[0].message.content / .reasoning_content
 *   resp.usage.{prompt_tokens, completion_tokens, total_tokens}
 *
 * HarmonyOS has no browser fetch; uses @ohos.net.http.
 */
import http from '@ohos.net.http';

export interface ChatMessage {
  role: string;
  content: string;
}

export interface ChatUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ChatResponse {
  content: string;
  reasoning_content: string;
  usage: ChatUsage;
}

// Raw wire shape (optional fields → null-safe access without Object casts).
interface RawMessage {
  content?: string;
  reasoning_content?: string;
}
interface RawChoice {
  message?: RawMessage;
}
interface RawUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}
interface RawResponse {
  choices?: RawChoice[];
  usage?: RawUsage;
}

export class LLMHTTPError extends Error {}

export class LlmClient {
  private baseUrl: string;
  private apiKey: string;
  private timeoutMs: number;

  constructor(baseUrl: string, apiKey: string, timeoutMs: number = 600000) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.apiKey = apiKey;
    this.timeoutMs = timeoutMs;
  }

  async create(
    model: string, messages: ChatMessage[], extra?: Record<string, Object>,
  ): Promise<ChatResponse> {
    const url = `${this.baseUrl}/chat/completions`;
    const body: Record<string, Object> = { 'model': model, 'messages': messages };
    if (extra !== undefined) {
      for (const k of Object.keys(extra)) {
        body[k] = extra[k];
      }
    }
    const request = http.createHttp();
    try {
      const options: http.HttpRequestOptions = {
        method: http.RequestMethod.POST,
        header: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${this.apiKey}`,
        },
        extraData: JSON.stringify(body),
        connectTimeout: this.timeoutMs,
        readTimeout: this.timeoutMs,
      };
      const resp = await request.request(url, options);
      const text = String(resp.result);
      if (resp.responseCode < 200 || resp.responseCode >= 300) {
        throw new LLMHTTPError(`HTTP ${resp.responseCode} from ${url}: ${text.slice(0, 2000)}`);
      }
      return this.normalize(JSON.parse(text) as RawResponse);
    } catch (e) {
      if (e instanceof LLMHTTPError) {
        throw e;
      }
      throw new LLMHTTPError(`request to ${url} failed: ${e}`);
    } finally {
      request.destroy();
    }
  }

  private normalize(raw: RawResponse): ChatResponse {
    const choices = raw.choices;
    const message = (choices !== undefined && choices.length > 0) ? choices[0].message : undefined;
    const usage = raw.usage;
    return {
      content: message?.content ?? '',
      reasoning_content: message?.reasoning_content ?? '',
      usage: {
        prompt_tokens: usage?.prompt_tokens ?? 0,
        completion_tokens: usage?.completion_tokens ?? 0,
        total_tokens: usage?.total_tokens ?? 0,
      },
    };
  }
}
