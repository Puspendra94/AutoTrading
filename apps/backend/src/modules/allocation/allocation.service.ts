import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { AllocationSnapshot } from '../../entities/allocation-snapshot.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { Ticker, TickerStatus } from '../../entities/ticker.entity';

@Injectable()
export class AllocationService {
  constructor(
    @InjectRepository(AllocationSnapshot)
    private readonly allocationRepo: Repository<AllocationSnapshot>,
    @InjectRepository(ProviderBalanceSnapshot)
    private readonly balanceRepo: Repository<ProviderBalanceSnapshot>,
    @InjectRepository(Ticker)
    private readonly tickerRepo: Repository<Ticker>,
  ) {}

  async rebalanceProviderPool(providerId: string) {
    const latestBalance = await this.balanceRepo.findOne({
      where: { providerId },
      order: { syncedAt: 'DESC' },
    });
    const poolCapital = latestBalance ? Number(latestBalance.tradableBalance) : 10000;

    const activeTickers = await this.tickerRepo.find({
      where: { providerId, status: TickerStatus.ACTIVE },
    });

    if (activeTickers.length === 0) return [];

    // Equal-weight allocation policy (Section 6)
    const basePct = Number((100 / activeTickers.length).toFixed(2));
    const snapshots: AllocationSnapshot[] = [];

    for (const t of activeTickers) {
      const allocatedCapital = (poolCapital * basePct) / 100;
      const snapshot = this.allocationRepo.create({
        providerId,
        tickerId: t.id,
        allocatedCapital,
        allocationPct: basePct,
      });
      snapshots.push(await this.allocationRepo.save(snapshot));
    }

    return snapshots;
  }
}
