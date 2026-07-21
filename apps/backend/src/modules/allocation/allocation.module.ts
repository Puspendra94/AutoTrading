import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { AllocationSnapshot } from '../../entities/allocation-snapshot.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { Ticker } from '../../entities/ticker.entity';
import { AllocationService } from './allocation.service';

@Module({
  imports: [TypeOrmModule.forFeature([AllocationSnapshot, ProviderBalanceSnapshot, Ticker])],
  providers: [AllocationService],
  exports: [AllocationService],
})
export class AllocationModule {}
