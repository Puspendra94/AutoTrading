/**
 * Parity runner: executes the REAL backend StrategyEvaluatorService on an input JSON so the
 * Python port (worker/strategy/evaluator.py) can be diffed against it. Not part of the app.
 *
 *   npx ts-node -T tools/parity_evaluator_runner.ts <input.json>
 *
 * input.json: { candles: [{close, timestamp(ms)}], params: {...}, policy: {...} }
 * stdout: JSON.stringify(BacktestPerformanceMetrics)
 */
import * as fs from 'fs';
import { StrategyEvaluatorService } from '../src/modules/strategy/strategy-evaluator.service';

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const candles = input.candles.map((c: any) => ({ close: c.close, timestamp: new Date(c.timestamp) }));
const svc = new StrategyEvaluatorService();
const result = svc.evaluateStrategy(candles as any, input.params, input.policy);
process.stdout.write(JSON.stringify(result));
