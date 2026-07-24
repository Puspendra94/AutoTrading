import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { LlmCostLog } from '../../entities/llm-cost-log.entity';
import { LlmService } from './llm.service';
import { LlmController } from './llm.controller';
import { LlmChainBuilder } from './llm-chain.builder';

@Module({
  imports: [TypeOrmModule.forFeature([LlmCostLog])],
  providers: [LlmService, LlmChainBuilder],
  controllers: [LlmController],
  exports: [LlmService],
})
export class LlmModule {}
