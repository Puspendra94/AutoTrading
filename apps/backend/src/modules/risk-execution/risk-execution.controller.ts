import { Controller, Get, Post, Body, Param, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { ExecutionService } from './execution.service';
import { PositionSide } from '../../entities/position.entity';

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
