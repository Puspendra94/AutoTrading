import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { LlmCostLog } from '../../entities/llm-cost-log.entity';
import { LlmService } from './llm.service';

@Module({
  imports: [TypeOrmModule.forFeature([LlmCostLog])],
  providers: [LlmService],
  exports: [LlmService],
})
export class LlmModule {}
