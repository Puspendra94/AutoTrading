import { RiskGateService } from './risk-gate.service';
import { PositionSide } from '../../entities/position.entity';

describe('RiskGateService', () => {
  let service: RiskGateService;
  let mockRiskLimitRepo: any;
  let mockDailyLossRepo: any;
  let mockPositionRepo: any;
  let mockTickerRepo: any;
  let mockBalanceRepo: any;
  let mockAlertRepo: any;

  beforeEach(() => {
    mockRiskLimitRepo = { findOne: jest.fn(), create: jest.fn((x) => x) };
    mockDailyLossRepo = { findOne: jest.fn(), create: jest.fn((x) => x), save: jest.fn() };
    mockPositionRepo = { count: jest.fn() };
    mockTickerRepo = { findOne: jest.fn() };
    mockBalanceRepo = { findOne: jest.fn() };
    mockAlertRepo = { create: jest.fn(), save: jest.fn() };

    service = new RiskGateService(
      mockRiskLimitRepo,
      mockDailyLossRepo,
      mockPositionRepo,
      mockTickerRepo,
      mockBalanceRepo,
      mockAlertRepo,
    );
  });

  it('should approve order when risk checks pass', async () => {
    mockTickerRepo.findOne.mockResolvedValue({ id: 't1', providerId: 'p1' });
    mockRiskLimitRepo.findOne.mockResolvedValue({
      dailyLossLimitPct: 2.0,
      maxConcurrentPositionsPerTicker: 1,
      probationSizePct: 25.0,
    });
    mockDailyLossRepo.findOne.mockResolvedValue({
      limitBreached: false,
      realizedPl: 0,
      unrealizedPl: 0,
    });
    mockBalanceRepo.findOne.mockResolvedValue({ tradableBalance: 10000 });
    mockPositionRepo.count.mockResolvedValue(0);

    const result = await service.evaluateOrderRiskGate({
      tickerId: 't1',
      side: PositionSide.LONG,
      price: 100,
    });

    expect(result.approved).toBe(true);
    expect(result.allowedQuantity).toBeGreaterThan(0);
    expect(result.isProbation).toBe(true);
  });

  it('should reject order when max concurrent positions limit reached', async () => {
    mockTickerRepo.findOne.mockResolvedValue({ id: 't1', providerId: 'p1' });
    mockRiskLimitRepo.findOne.mockResolvedValue({
      dailyLossLimitPct: 2.0,
      maxConcurrentPositionsPerTicker: 1,
      probationSizePct: 25.0,
    });
    mockDailyLossRepo.findOne.mockResolvedValue({
      limitBreached: false,
      realizedPl: 0,
      unrealizedPl: 0,
    });
    mockBalanceRepo.findOne.mockResolvedValue({ tradableBalance: 10000 });
    mockPositionRepo.count.mockResolvedValue(1); // Already 1 open position

    const result = await service.evaluateOrderRiskGate({
      tickerId: 't1',
      side: PositionSide.LONG,
      price: 100,
    });

    expect(result.approved).toBe(false);
    expect(result.reason).toContain('Max concurrent positions limit reached');
  });
});
