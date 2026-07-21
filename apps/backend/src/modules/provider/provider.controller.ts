import { Controller, Get, Post, Patch, Body, Param, UseGuards, Request } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { ProviderService } from './provider.service';
import { ProviderType, TradingMode } from '../../entities/provider.entity';

@Controller('providers')
@UseGuards(AuthGuard('jwt'))
export class ProviderController {
  constructor(private readonly providerService: ProviderService) {}

  @Get()
  async listProviders(@Request() req: any) {
    return this.providerService.listProviders(req.user.id);
  }

  @Post()
  async createProvider(
    @Request() req: any,
    @Body()
    body: {
      name: string;
      type: ProviderType;
      apiKey?: string;
      apiSecret?: string;
      tradingMode?: TradingMode;
      useTestnet?: boolean;
    },
  ) {
    return this.providerService.createProvider(req.user.id, body);
  }

  @Patch(':id/toggle')
  async toggleTrading(@Param('id') id: string, @Body() body: { enabled: boolean }) {
    return this.providerService.toggleTrading(id, body.enabled);
  }

  @Patch(':id/trading-mode')
  async updateTradingMode(
    @Param('id') id: string,
    @Body() body: { tradingMode?: TradingMode; useTestnet?: boolean },
  ) {
    return this.providerService.updateTradingMode(id, body.tradingMode, body.useTestnet);
  }

  @Get(':id/balance')
  async getBalance(@Param('id') id: string) {
    return this.providerService.getLatestBalance(id);
  }

  @Post(':id/sync-balance')
  async syncBalance(@Param('id') id: string) {
    return this.providerService.syncBalance(id);
  }
}
