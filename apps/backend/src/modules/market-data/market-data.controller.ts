import { Controller, Get, Post, Body, Param, Query, UseGuards, Logger } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { MarketDataService } from './market-data.service';
import { MarketStreamService } from './market-stream.service';

@Controller('market-data')
@UseGuards(AuthGuard('jwt'))
export class MarketDataController {
  private readonly logger = new Logger(MarketDataController.name);

  constructor(
    private readonly marketDataService: MarketDataService,
    private readonly marketStreamService: MarketStreamService,
  ) {}

  @Get('tickers')
  async listTickers(@Query('providerId') providerId?: string) {
    return this.marketDataService.listTickers(providerId);
  }

  @Post('tickers')
  async addTicker(@Body() body: { providerId: string; symbol: string; interval?: string }) {
    const ticker = await this.marketDataService.addTicker(body.providerId, body.symbol, body.interval);
    this.runBackfillAndStartStream(ticker.id);
    return ticker;
  }

  @Get('tickers/:id/candles')
  async getCandles(@Param('id') id: string, @Query('limit') limit?: string) {
    return this.marketDataService.getCandles(id, limit ? parseInt(limit, 10) : 200);
  }

  @Post('tickers/:id/backfill')
  async backfill(@Param('id') id: string) {
    const result = await this.marketDataService.backfillHistoricalData(id);
    if (!result.failed) {
      const ticker = await this.marketDataService.getTickerById(id);
      this.marketStreamService.startStream(ticker);
    }
    return result;
  }

  private runBackfillAndStartStream(tickerId: string) {
    this.marketDataService
      .backfillHistoricalData(tickerId)
      .then(async (result) => {
        if (!result.failed) {
          const ticker = await this.marketDataService.getTickerById(tickerId);
          this.marketStreamService.startStream(ticker);
        }
      })
      .catch((err) => this.logger.error(`Backfill/stream startup failed for ticker ${tickerId}: ${err.message}`));
  }
}
