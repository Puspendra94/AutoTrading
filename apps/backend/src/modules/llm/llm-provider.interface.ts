export interface LlmCompletionOptions {
  model?: string;
  maxTokens?: number;
  temperature?: number;
}

export interface LlmCompletionResponse {
  content: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  model: string;
  /** Which backend actually served this completion — set by whichever entry in the
   * LLM_MODELS fallback chain succeeded, not necessarily the first/primary one. */
  provider?: 'direct_api' | 'bedrock' | 'deepseek';
}

export interface LlmStructuredCompletionResponse<T> {
  data: T;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  model: string;
  provider?: 'direct_api' | 'bedrock' | 'deepseek';
}

export interface LLMProvider {
  generateCompletion(prompt: string, options?: LlmCompletionOptions): Promise<LlmCompletionResponse>;
}
