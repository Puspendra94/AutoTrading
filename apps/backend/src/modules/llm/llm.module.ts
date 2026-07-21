import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { LlmCostLog } from '../../entities/llm-cost-log.entity';
import { LlmService } from './llm.service';
import { LlmController } from './llm.controller';

@Module({
  imports: [TypeOrmModule.forFeature([LlmCostLog])],
  providers: [LlmService],
  controllers: [LlmController],
  exports: [LlmService],
})
export class LlmModule {}
