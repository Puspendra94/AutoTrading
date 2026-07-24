import { Injectable, Logger } from '@nestjs/common';
import { AnthropicBedrockMantle } from '@anthropic-ai/bedrock-sdk';
import { LLMProvider, LlmCompletionOptions, LlmCompletionResponse } from '../llm-provider.interface';

const DEFAULT_MODEL = 'claude-opus-4-8';

const PRICING: Record<string, { input: number; output: number }> = {
  'claude-opus-4-8': { input: 5.0, output: 25.0 },
  'claude-sonnet-5': { input: 3.0, output: 15.0 },
  'claude-haiku-4-5': { input: 1.0, output: 5.0 },
};

@Injectable()
export class BedrockProvider implements LLMProvider {
  private readonly logger = new Logger(BedrockProvider.name);
  private readonly client: AnthropicBedrockMantle | null;

  constructor() {
    const region = process.env.AWS_REGION;
    const accessKey = process.env.AWS_ACCESS_KEY_ID;
    const secretKey = process.env.AWS_SECRET_ACCESS_KEY;

    if (!region) {
      this.client = null;
      return;
    }
    // Only pass explicit SigV4 credentials when both are actually set — otherwise
    // let the client fall through to its default AWS credential chain (profile,
    // instance role, etc.) rather than forcing empty-string credentials.
    this.client = new AnthropicBedrockMantle({
      awsRegion: region,
      ...(accessKey && secretKey
        ? { awsAccessKey: accessKey, awsSecretAccessKey: secretKey }
        : {}),
    });
  }

  async generateCompletion(prompt: string, options?: LlmCompletionOptions): Promise<LlmCompletionResponse> {
    if (!this.client) {
      throw new Error('AWS_REGION not configured — cannot call Claude via Amazon Bedrock.');
    }
    const model = options?.model || DEFAULT_MODEL;
    // Bedrock model IDs take an `anthropic.` prefix — never a first-party bare ID.
    const bedrockModelId = model.startsWith('anthropic.') ? model : `anthropic.${model}`;

    const response = await this.client.messages.create({
      model: bedrockModelId,
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
