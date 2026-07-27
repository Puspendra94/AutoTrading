/**
 * DSL parity runner: executes the REAL backend DSL interpreter (simulateFromIR) on an input JSON
 * so the Python port (worker/strategy/dsl/interpreter.py) can be diffed against it. Not part of
 * the app.
 *
 *   npx ts-node -T tools/parity_dsl_runner.ts <input.json>
 *
 * input.json: { candles: [{open,high,low,close,volume,timestamp(ms)}], ir: StrategyIR }
 * stdout: JSON.stringify({ trades, maxDrawdown, drawdownDuration })
 */
import * as fs from 'fs';
import { simulateFromIR } from '../src/modules/strategy/dsl/interpreter';

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const result = simulateFromIR(input.candles, input.ir);
process.stdout.write(JSON.stringify(result));
