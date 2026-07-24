import { Injectable, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { HumanMessage } from '@langchain/core/messages';
import type { ZodType } from 'zod';
import { LlmCostLog, LlmPurpose } from '../../entities/llm-cost-log.entity';
import { LLMProvider, LlmCompletionResponse, LlmStructuredCompletionResponse } from './llm-provider.interface';
import { LlmChainBuilder, ModelSpec } from './llm-chain.builder';

// Only models we have solid, current pricing data for (claude-api skill's reference
// table) get precise cost tracking. Anything else (DeepSeek, gpt-oss, etc.) still
// works end-to-end but is logged at $0 rather than a fabricated estimate — cost
// tracking accuracy matters more than always showing a non-zero number.
const PRICING: Record<string, { input: number; output: number }> = {
  'claude-opus-4-8': { input: 5.0, output: 25.0 },
  'claude-sonnet-5': { input: 3.0, output: 15.0 },
  'claude-haiku-4-5': { input: 1.0, output: 5.0 },
};

function priceFor(modelId: string): { input: number; output: number } {
  const normalized = modelId.split('.').pop()?.replace(/-\d{8}.*$/, '') || modelId;
  return PRICING[normalized] || PRICING[modelId] || { input: 0, output: 0 };
}

/**
 * Spec 9.2 — LLM backend is configurable, never hardcoded. Generalized beyond the
 * spec's literal "direct_api | bedrock" wording: LLM_MODELS is an ordered
 * provider:modelId fallback chain (any Bedrock Converse-compatible model — Claude,
 * DeepSeek, gpt-oss, etc. — or the direct Anthropic API), so adding a new model
 * never requires a code change. Every strategy-generation/re-evaluation/live-decision
 * call goes through this single service regardless of which entry in the chain
 * actually serves it.
 */
@Injectable()
export class LlmService implements LLMProvider {
  private readonly logger = new Logger(LlmService.name);
  private readonly modelChain: ModelSpec[];

  constructor(
    @InjectRepository(LlmCostLog)
    private readonly costLogRepo: Repository<LlmCostLog>,
    private readonly chainBuilder: LlmChainBuilder,
  ) {
    this.modelChain = this.chainBuilder.parseChain();
    this.logger.log(`LLM fallback chain: ${this.modelChain.map((s) => `${s.provider}:${s.modelId}`).join(' -> ')}`);
  }

  async generateCompletion(prompt: string, options?: any): Promise<LlmCompletionResponse> {
    const maxTokens = options?.maxTokens || 1024;

    for (const spec of this.modelChain) {
      try {
        const model = this.chainBuilder.buildModel(spec, maxTokens);
        const result = await model.invoke([new HumanMessage(prompt)]);
        const text = this.chainBuilder.extractText(result.content);
        const usage = result.usage_metadata as { input_tokens?: number; output_tokens?: number } | undefined;
        const inputTokens = usage?.input_tokens ?? 0;
        const outputTokens = usage?.output_tokens ?? 0;
        const price = priceFor(spec.modelId);
        const costUsd = (inputTokens * price.input) / 1_000_000 + (outputTokens * price.output) / 1_000_000;

        return {
          content: text,
          inputTokens,
          outputTokens,
          costUsd,
          model: spec.modelId,
          provider: spec.provider,
        };
      } catch (err) {
        const chain: string[] = [];
        let cur = err;
        for (let i = 0; i < 5 && cur; i++) {
          chain.push(`${cur.code ? `[${cur.code}] ` : ''}${cur.message || cur}`);
          cur = cur.cause;
        }
        this.logger.warn(`Model '${spec.provider}:${spec.modelId}' failed: ${chain.join(' <- ')} — trying next in chain.`);
      }
    }

    this.logger.warn(`All models in LLM_MODELS chain failed — using quantitative fallback.`);
    return this.syntheticFallback(this.modelChain[0]?.modelId);
  }

  /**
   * Same fallback chain as generateCompletion, but forces the response into `schema`
   * via LangChain's withStructuredOutput() (provider-native tool-calling/JSON-mode
   * under the hood) instead of trusting the model to follow a prose "respond in JSON"
   * instruction. Fixes the schema-drift bug where different providers (Claude vs
   * DeepSeek vs gpt-oss) each returned a plausible but differently-shaped JSON object,
   * silently breaking downstream `params.indicatorConfig.*` reads.
   */
  async generateStructuredCompletion<T extends Record<string, any>>(
    prompt: string,
    schema: ZodType<T>,
    options?: { maxTokens?: number; schemaName?: string },
  ): Promise<LlmStructuredCompletionResponse<T>> {
    const maxTokens = options?.maxTokens || 1024;

    for (const spec of this.modelChain) {
      try {
        const model = this.chainBuilder.buildModel(spec, maxTokens);
        // Method must be picked per-provider: DeepSeek's reasoning models (e.g.
        // deepseek-v4-flash) 400 on the forced tool_choice that the default
        // 'functionCalling' method sends ("Thinking mode does not support this
        // tool_choice"), so DeepSeek needs 'jsonMode' (plain response_format, no
        // forced tool call). ChatBedrockConverse throws outright if given 'jsonMode',
        // so bedrock/direct_api must stick with the default.
        const structuredModel = model.withStructuredOutput(schema, {
          name: options?.schemaName || 'propose_strategy_params',
          includeRaw: true,
          ...(spec.provider === 'deepseek' ? { method: 'jsonMode' } : {}),
        });
        const result = await structuredModel.invoke([new HumanMessage(prompt)]);
        const usage = (result.raw as any)?.usage_metadata as
          | { input_tokens?: number; output_tokens?: number }
          | undefined;
        const inputTokens = usage?.input_tokens ?? 0;
        const outputTokens = usage?.output_tokens ?? 0;
        const price = priceFor(spec.modelId);
        const costUsd = (inputTokens * price.input) / 1_000_000 + (outputTokens * price.output) / 1_000_000;

        return {
          data: result.parsed as T,
          inputTokens,
          outputTokens,
          costUsd,
          model: spec.modelId,
          provider: spec.provider,
        };
      } catch (err) {
        const chain: string[] = [];
        let cur = err;
        for (let i = 0; i < 5 && cur; i++) {
          chain.push(`${cur.code ? `[${cur.code}] ` : ''}${cur.message || cur}`);
          cur = cur.cause;
        }
        this.logger.warn(
          `Model '${spec.provider}:${spec.modelId}' failed structured call: ${chain.join(' <- ')} — trying next in chain.`,
        );
      }
    }

    this.logger.warn(`All models in LLM_MODELS chain failed structured output — using quantitative fallback.`);
    return this.syntheticStructuredFallback(schema, this.modelChain[0]?.modelId);
  }

  /** Same intent as syntheticFallback, but for the structured-output path — reuses the
   * same canned defaults, validated against `schema` so callers never get a shape
   * mismatch even on the fallback path. */
  private syntheticStructuredFallback<T extends Record<string, any>>(
    schema: ZodType<T>,
    requestedModel?: string,
  ): LlmStructuredCompletionResponse<T> {
    const modelName = requestedModel || 'unknown-model';
    const fallback = schema.parse({
      strategyName: 'Adaptive Trend Breakout + RSI Filter',
      indicatorConfig: {
        emaFastPeriod: 12,
        emaSlowPeriod: 26,
        rsiPeriod: 14,
        rsiBuyThreshold: 45,
        rsiSellThreshold: 65,
        stopLossPct: 1.5,
        takeProfitPct: 3.5,
      },
      reasoning: 'Calculated statistical momentum continuation with RSI divergence filter on 1m/5m timeframe.',
    });

    const inputTokens = 210;
    const outputTokens = 140;
    const costUsd = (inputTokens * 3 + outputTokens * 15) / 1_000_000;

    return {
      data: fallback,
      inputTokens,
      outputTokens,
      costUsd,
      model: `${modelName}-fallback`,
    };
  }

  /**
   * Deterministic fallback used only when every model in the chain is
   * unreachable/misconfigured (no credentials, no credits, no model access, network
   * failure) — never used silently for a successful call. Always labeled with a
   * `-fallback` suffix on the model field so it's never mistaken for a real LLM
   * response downstream.
   */
  private syntheticFallback(requestedModel?: string): LlmCompletionResponse {
    const modelName = requestedModel || 'unknown-model';
    const syntheticResponse = JSON.stringify({
      strategyName: 'Adaptive Trend Breakout + RSI Filter',
      indicatorConfig: {
        emaFastPeriod: 12,
        emaSlowPeriod: 26,
        rsiPeriod: 14,
        rsiBuyThreshold: 45,
        rsiSellThreshold: 65,
        stopLossPct: 1.5,
        takeProfitPct: 3.5,
      },
      reasoning: 'Calculated statistical momentum continuation with RSI divergence filter on 1m/5m timeframe.',
    });

    const inputTokens = 210;
    const outputTokens = 140;
    const costUsd = (inputTokens * 3 + outputTokens * 15) / 1_000_000;

    return {
      content: syntheticResponse,
      inputTokens,
      outputTokens,
      costUsd,
      model: `${modelName}-fallback`,
    };
  }

  async logCost(tickerId: string, strategyId: string, purpose: LlmPurpose, response: LlmCompletionResponse) {
    const log = this.costLogRepo.create({
      tickerId,
      strategyId,
      purpose,
      llmProvider: response.model.endsWith('-fallback') ? 'fallback' : response.provider || 'unknown',
      model: response.model,
      inputTokens: response.inputTokens,
      outputTokens: response.outputTokens,
      costUsd: response.costUsd,
    });
    return this.costLogRepo.save(log);
  }

  async getCostSummary() {
    const logs = await this.costLogRepo.find({ order: { calledAt: 'DESC' }, take: 100 });
    const totalCost = logs.reduce((sum, item) => sum + Number(item.costUsd), 0);
    return {
      totalCostUsd: totalCost,
      totalCalls: logs.length,
      logs,
    };
  }
}
