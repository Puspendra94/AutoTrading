import { Controller, Get, Post, Body, Param, Query, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { MarketDataService } from './market-data.service';

@Controller('market-data')
@UseGuards(AuthGuard('jwt'))
export class MarketDataController {
  constructor(private readonly marketDataService: MarketDataService) {}

  @Get('tickers')
  async listTickers(@Query('providerId') providerId?: string) {
    return this.marketDataService.listTickers(providerId);
  }

  @Post('tickers')
  async addTicker(@Body() body: { providerId: string; symbol: string; interval?: string }) {
    return this.marketDataService.addTicker(body.providerId, body.symbol, body.interval);
  }

  @Get('tickers/:id/candles')
  async getCandles(@Param('id') id: string, @Query('limit') limit?: string) {
    return this.marketDataService.getCandles(id, limit ? parseInt(limit, 10) : 200);
  }

  @Post('tickers/:id/backfill')
  async backfill(@Param('id') id: string) {
    return this.marketDataService.backfillHistoricalData(id);
  }
}
