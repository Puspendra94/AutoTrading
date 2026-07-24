import { Injectable, Logger } from '@nestjs/common';
import Anthropic from '@anthropic-ai/sdk';
import { LLMProvider, LlmCompletionOptions, LlmCompletionResponse } from '../llm-provider.interface';

// Claude 3.5 Sonnet (the prior hardcoded default) retired 2025-10-28 — every call
// would have 404'd regardless of API credits. claude-opus-4-8 is the current default
// per spec 9.2's "direct Anthropic API" backend; override per-call via options.model.
const DEFAULT_MODEL = 'claude-opus-4-8';

// Pricing per current model catalog (USD per 1M tokens). Extend as new models are used.
const PRICING: Record<string, { input: number; output: number }> = {
  'claude-opus-4-8': { input: 5.0, output: 25.0 },
  'claude-sonnet-5': { input: 3.0, output: 15.0 },
  'claude-haiku-4-5': { input: 1.0, output: 5.0 },
};

@Injectable()
export class DirectAnthropicProvider implements LLMProvider {
  private readonly logger = new Logger(DirectAnthropicProvider.name);
  private readonly client: Anthropic | null;

  constructor() {
    const apiKey = (process.env.ANTHROPIC_API_KEY || '').replace(/^["']|["']$/g, '').trim();
    this.client = apiKey.length > 10 ? new Anthropic({ apiKey }) : null;
  }

  async generateCompletion(prompt: string, options?: LlmCompletionOptions): Promise<LlmCompletionResponse> {
    if (!this.client) {
      throw new Error('ANTHROPIC_API_KEY not configured — cannot call the direct Anthropic API.');
    }
    const model = options?.model || DEFAULT_MODEL;

    const response = await this.client.messages.create({
      model,
      max_tokens: options?.maxTokens || 1024,
      messages: [{ role: 'user', content: prompt }],
    });

    const text = response.content.find((b) => b.type === 'text')?.text || '';
    const inputTokens = response.usage.input_tokens;
    const outputTokens = response.usage.output_tokens;
    const price = PRICING[model] || PRICING[DEFAULT_MODEL];
    const costUsd = (inputTokens * price.input) / 1_000_000 + (outputTokens * price.output) / 1_000_000;

    return { content: text, inputTokens, outputTokens, costUsd, model };
  }
}
