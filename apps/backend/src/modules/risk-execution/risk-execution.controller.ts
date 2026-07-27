import { Controller, Get, Post, Body, Param, Query, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { ExecutionService } from './execution.service';
import { PositionSide, TradeMode, TradeNetwork } from '../../entities/position.entity';

@Controller('execution')
@UseGuards(AuthGuard('jwt'))
export class RiskExecutionController {
  constructor(private readonly executionService: ExecutionService) {}

  @Get('positions/open')
  async getOpenPositions() {
    return this.executionService.getOpenPositions();
  }

  // Open positions annotated against the live Binance account (paper/live + exchangeVerified)
  // so the dashboard can flag simulated and stale/ghost trades. Hits the exchange, so the
  // dashboard polls this on a slow interval rather than per-tick.
  @Get('positions/open/reconciled')
  async getOpenPositionsReconciled() {
    return this.executionService.getOpenPositionsReconciled();
  }

  @Get('positions/all')
  async getAllPositions() {
    return this.executionService.getAllPositions();
  }

  // The Live Trades feed: open + closed trades matching the active Mode + Network, newest
  // first. `mode` defaults to paper; `network` is only applied for live (paper is
  // network-agnostic). Drives the dashboard's Live Trades panel.
  @Get('trades')
  async getTrades(@Query('mode') mode?: string, @Query('network') network?: string) {
    const tradeMode = mode === 'live' ? TradeMode.LIVE : TradeMode.PAPER;
    const tradeNetwork = network === 'mainnet' ? TradeNetwork.MAINNET : network === 'testnet' ? TradeNetwork.TESTNET : undefined;
    return this.executionService.getTradesForView(tradeMode, tradeNetwork);
  }

  // Simulated paper-trading performance (equity, ROI, realized/unrealized P/L, available balance,
  // win rate) — drives the dashboard's paper-only performance panel.
  @Get('paper-summary')
  async getPaperSummary() {
    return this.executionService.getPaperSummary();
  }

  // Paper-only: opens a long on the ticker's active strategy at `price`, or closes the open one.
  @Post('test-trade')
  async testTrade(@Body() body: { tickerId: string; price: number }) {
    return this.executionService.placeTestPaperTrade(body.tickerId, body.price);
  }

  @Post('trade')
  async placeTrade(
    @Body() body: { tickerId: string; side: PositionSide; price: number; strategyId?: string },
  ) {
    return this.executionService.executeTradeSignal(body.tickerId, body.side, body.price, body.strategyId);
  }

  @Post('positions/:id/close')
  async closePosition(@Param('id') id: string, @Body() body: { exitPrice: number }) {
    return this.executionService.closePosition(id, body.exitPrice);
  }
}
