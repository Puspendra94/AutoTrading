/**
 * Phase 5 end-to-end DRY RUN (non-destructive): calls the REAL LLM with the open-ended DSL
 * generation prompt, parses + validates the returned rule tree, and backtests it on REAL BTCUSDT
 * candles through the same evaluator the gate uses. Writes NOTHING (no strategy is persisted or
 * promoted) — it only proves the open-ended generation path works end-to-end with a live model.
 *
 *   npx ts-node -T tools/dsl_generate_dryrun.ts [tickerId] [interval] [attempts]
 */
import dataSource from '../src/database/data-source';
import { LlmChainBuilder } from '../src/modules/llm/llm-chain.builder';
import { LlmService } from '../src/modules/llm/llm.service';
import { StrategyEvaluatorService } from '../src/modules/strategy/strategy-evaluator.service';
import { StrategyGenSchema } from '../src/modules/llm/schemas/strategy-gen.schema';
import { STRATEGY_GRAMMAR_PROMPT } from '../src/modules/strategy/dsl/grammar-prompt';
import { validateIR, resolveIR, describeStrategy, countIRParameters, StrategyIR } from '../src/modules/strategy/dsl/strategy-ir';

const TICKER = process.argv[2] || '4ed6d352-658f-4bd6-83cc-9f0416145073'; // BTCUSDT
const INTERVAL = process.argv[3] || '1h';
const ATTEMPTS = parseInt(process.argv[4] || '3', 10);
const CANDLE_LIMIT = 10000;

const POLICY: any = { minSharpe: 1.0, maxDrawdownPct: 20, minProfitFactor: 1.3, minTradeCount: 20, maxParameterCount: 8 };

async function loadCandles(tickerId: string, interval: string, limit: number) {
  const m = interval.match(/^(\d+)([mhdw])$/i)!;
  const unit = { m: 'minutes', h: 'hours', d: 'days', w: 'weeks' }[m[2].toLowerCase()];
  const rows = await dataSource.query(
    `SELECT "timestamp", open, high, low, close, volume FROM (
       SELECT time_bucket(INTERVAL '${parseInt(m[1], 10)} ${unit}', "timestamp") AS "timestamp",
         first(open,"timestamp") AS open, max(high) AS high, min(low) AS low,
         last(close,"timestamp") AS close, sum(volume) AS volume
       FROM "Algo_Trading"."ohlcv_data" WHERE ticker_id = $1 GROUP BY 1 ORDER BY 1 DESC LIMIT $2
     ) t ORDER BY "timestamp" ASC`,
    [tickerId, limit],
  );
  return rows.map((c: any) => ({
    timestamp: new Date(c.timestamp), open: Number(c.open), high: Number(c.high),
    low: Number(c.low), close: Number(c.close), volume: Number(c.volume),
  }));
}

async function main() {
  await dataSource.initialize();
  const candles = await loadCandles(TICKER, INTERVAL, CANDLE_LIMIT);
  console.log(`Loaded ${candles.length} ${INTERVAL} candles for ${TICKER} (latest close ${candles.at(-1)?.close}).`);

  const llm = new LlmService({} as any, new LlmChainBuilder());
  const evaluator = new StrategyEvaluatorService();

  const prompt = `Design an automated trading strategy for BTCUSDT (Interval: ${INTERVAL}, Latest Price: ${candles.at(-1)?.close}).

${STRATEGY_GRAMMAR_PROMPT}

Parameter budget for THIS strategy: at most ${POLICY.maxParameterCount} distinct tunable knobs.
Prioritize a positive out-of-sample Sharpe (>= 1) and profit factor (>= 1.3) over trade frequency.

Respond with strategyName, reasoning, and the "strategy" field (the entry/exit/risk rule tree as a JSON string).`;

  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    console.log(`\n──── Attempt ${attempt}/${ATTEMPTS} ────`);
    let res;
    try {
      res = await llm.generateStructuredCompletion(prompt, StrategyGenSchema, { schemaName: 'propose_strategy', maxTokens: 4000 });
    } catch (e: any) {
      console.log(`❌ LLM call failed (all models): ${e.message}`);
      continue;
    }
    console.log(`Model: ${res.provider}:${res.model}  (in ${res.inputTokens} / out ${res.outputTokens} tok, $${res.costUsd.toFixed(5)})`);
    console.log(`strategyName: ${res.data.strategyName}`);

    const d: any = res.data;
    // Natural nested shape (entry/exit/risk fields); tolerate a wrapped `strategy` field too.
    let body: any = d;
    if (!d.entry && d.strategy != null) {
      try {
        body = typeof d.strategy === 'string' ? JSON.parse(d.strategy) : d.strategy;
      } catch (e: any) {
        console.log(`❌ strategy field did not parse as JSON: ${e.message}`);
        continue;
      }
    }
    const ir = { strategyName: d.strategyName, reasoning: d.reasoning || '', entry: body.entry, exit: body.exit, risk: body.risk } as StrategyIR;
    const errs = validateIR(ir);
    if (errs.length) {
      console.log(`❌ invalid rule tree: ${errs.slice(0, 5).join('; ')}`);
      console.log(`TREE: ${JSON.stringify(body)}`);
      continue;
    }
    console.log(`✓ valid rule tree — ${describeStrategy(ir)}`);
    console.log(`  entry: ${JSON.stringify(ir.entry)}`);
    console.log(`  exit:  ${JSON.stringify(ir.exit)}`);
    console.log(`  risk:  ${JSON.stringify(ir.risk)}`);

    const ev = evaluator.evaluateStrategy(candles as any, resolveIR(ir), POLICY);
    console.log(
      `  BACKTEST (out-of-sample): sharpe ${ev.sharpe}  PF ${ev.profitFactor}  maxDD ${ev.maxDrawdown}%  ` +
        `trades ${ev.tradeCount}  params ${ev.parameterCount}  return ${ev.totalReturnPct}%`,
    );
    console.log(`  GATE: ${ev.passedEvaluationGate ? '✅ PASSED (would be promoted live)' : '⛔ failed (would be discarded)'}`);
    if (ev.passedEvaluationGate) break;
  }

  await dataSource.destroy();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
