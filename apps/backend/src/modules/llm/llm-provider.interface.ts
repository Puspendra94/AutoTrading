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
}

export interface LLMProvider {
  generateCompletion(prompt: string, options?: LlmCompletionOptions): Promise<LlmCompletionResponse>;
}
