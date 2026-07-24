import { Injectable, Logger } from '@nestjs/common';
import { ChatAnthropic } from '@langchain/anthropic';
import { ChatBedrockConverse } from '@langchain/aws';
import { ChatDeepSeek } from '@langchain/deepseek';
import type { BaseChatModel } from '@langchain/core/language_models/chat_models';

const KNOWN_PROVIDERS = ['direct_api', 'bedrock', 'deepseek'] as const;
export type ProviderKind = (typeof KNOWN_PROVIDERS)[number];

export interface ModelSpec {
  provider: ProviderKind;
  modelId: string;
}

/**
 * Provider-agnostic model chain, configured entirely via LLM_MODELS — no code
 * change needed to add a new model within an already-supported provider (spec
 * 9.2's "LLM provider must be configurable" requirement, generalized beyond just
 * direct_api/bedrock). Adding a genuinely new *provider* (a new backend/auth
 * scheme) is a small addition here — one new branch in buildModel — while every
 * caller (LlmService, ai-lessons.service.ts, strategy-engine.service.ts) stays
 * untouched, since they only ever see the LLMProvider interface.
 *
 * Format: comma-separated "provider:modelId" pairs, tried in order as a fallback
 * chain — e.g. "bedrock:anthropic.claude-sonnet-4-5-20250929-v1:0,deepseek:deepseek-chat,direct_api:claude-opus-4-8".
 * `modelId` is passed through verbatim to the chosen backend — for Bedrock this
 * must be the exact Converse-API model ID (see `aws bedrock list-foundation-models`),
 * not the bare first-party Anthropic alias used by the Mantle endpoint.
 */
@Injectable()
export class LlmChainBuilder {
  private readonly logger = new Logger(LlmChainBuilder.name);

  parseChain(): ModelSpec[] {
    const raw = process.env.LLM_MODELS || 'direct_api:claude-opus-4-8';
    const specs = raw
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean)
      .map((entry) => {
        const idx = entry.indexOf(':');
        if (idx === -1) {
          throw new Error(`Invalid LLM_MODELS entry '${entry}' — expected 'provider:modelId'.`);
        }
        const provider = entry.slice(0, idx).trim();
        const modelId = entry.slice(idx + 1).trim();
        if (!KNOWN_PROVIDERS.includes(provider as ProviderKind)) {
          throw new Error(
            `Unknown provider '${provider}' in LLM_MODELS — must be one of: ${KNOWN_PROVIDERS.join(', ')}.`,
          );
        }
        return { provider: provider as ProviderKind, modelId };
      });
    if (specs.length === 0) {
      throw new Error('LLM_MODELS resolved to an empty chain.');
    }
    return specs;
  }

  buildModel(spec: ModelSpec, maxTokens: number): BaseChatModel {
    switch (spec.provider) {
      case 'direct_api': {
        const apiKey = (process.env.ANTHROPIC_API_KEY || '').replace(/^["']|["']$/g, '').trim();
        if (!apiKey || apiKey.length < 10) {
          throw new Error('ANTHROPIC_API_KEY not configured for direct_api provider.');
        }
        return new ChatAnthropic({ apiKey, model: spec.modelId, maxTokens });
      }

      case 'deepseek': {
        const apiKey = (process.env.DEEPSEEK_API_KEY || '').trim();
        if (!apiKey || apiKey.length < 10) {
          throw new Error('DEEPSEEK_API_KEY not configured for deepseek provider.');
        }
        return new ChatDeepSeek({ apiKey, model: spec.modelId, maxTokens });
      }

      case 'bedrock': {
        const region = process.env.AWS_REGION;
        if (!region) {
          throw new Error('AWS_REGION not configured for bedrock provider.');
        }
        const bearerToken = (process.env.AWS_BEDROCK_API_KEY || '').trim();
        const accessKey = process.env.AWS_ACCESS_KEY_ID;
        const secretKey = process.env.AWS_SECRET_ACCESS_KEY;
        const sessionToken = process.env.AWS_SESSION_TOKEN;

        return new ChatBedrockConverse({
          model: spec.modelId,
          region,
          maxTokens,
          ...(bearerToken
            ? { bedrockBearerToken: bearerToken }
            : accessKey && secretKey
              ? {
                  credentials: {
                    accessKeyId: accessKey,
                    secretAccessKey: secretKey,
                    ...(sessionToken ? { sessionToken } : {}),
                  },
                }
              : {}),
        });
      }
    }
  }

  /** Extracts plain text from LangChain's MessageContent union (string, or an
   * array of content blocks for multimodal-capable providers). */
  extractText(content: unknown): string {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content
        .map((block: any) => (typeof block === 'string' ? block : block?.text || ''))
        .join('');
    }
    return '';
  }
}
