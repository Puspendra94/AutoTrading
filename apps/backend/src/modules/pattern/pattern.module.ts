import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { PatternSignal } from '../../entities/pattern-signal.entity';
import { PatternDecision } from '../../entities/pattern-decision.entity';
import { PatternService } from './pattern.service';
import { PatternController } from './pattern.controller';

// Read-only module: the worker's feature engine writes pattern_signals, this serves them.
// Nothing here decides or executes anything, so it is safe to mount regardless of TRADING_BRAIN
// — with the strategy brain active the table is simply empty.
@Module({
  imports: [TypeOrmModule.forFeature([PatternSignal, PatternDecision])],
  providers: [PatternService],
  controllers: [PatternController],
  exports: [PatternService],
})
export class PatternModule {}
