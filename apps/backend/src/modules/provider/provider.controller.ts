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

  // Live account straight from Binance — real balances + open orders for the connected view.
  @Get(':id/account')
  async getLiveAccount(@Param('id') id: string) {
    return this.providerService.getLiveAccount(id);
  }

  // Disconnect — remove stored credentials, dropping back to the not-connected form.
  @Post(':id/disconnect')
  async disconnect(@Param('id') id: string) {
    return this.providerService.disconnect(id);
  }

  // Risk-limit guardrails, read/edited from the Profile page.
  @Get(':id/risk-limit')
  async getRiskLimit(@Param('id') id: string) {
    return this.providerService.getRiskLimit(id);
  }

  @Patch(':id/risk-limit')
  async updateRiskLimit(
    @Param('id') id: string,
    @Body()
    body: {
      dailyLossLimitPct?: number;
      maxConcurrentPositionsPerTicker?: number;
      probationSizePct?: number;
      probationTradesCount?: number;
      resetBoundary?: any;
      hardStopLossPct?: number | null;
      hardTakeProfitPct?: number | null;
    },
  ) {
    return this.providerService.updateRiskLimit(id, body);
  }

  @Post(':id/sync-balance')
  async syncBalance(@Param('id') id: string) {
    return this.providerService.syncBalance(id);
  }

  @Post(':id/kill-switch')
  async triggerKillSwitch(@Param('id') id: string, @Body() body: { reason?: string }) {
    return this.providerService.triggerKillSwitch(id, body.reason || 'Manually triggered via API.');
  }

  @Post(':id/kill-switch/clear')
  async clearKillSwitch(@Param('id') id: string) {
    return this.providerService.clearKillSwitch(id);
  }
}
