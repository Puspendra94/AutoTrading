import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { LlmCostLog } from '../../entities/llm-cost-log.entity';
import { LlmService } from './llm.service';
import { LlmController } from './llm.controller';
import { DirectAnthropicProvider } from './providers/direct-anthropic.provider';
import { BedrockProvider } from './providers/bedrock.provider';

@Module({
  imports: [TypeOrmModule.forFeature([LlmCostLog])],
  providers: [LlmService, DirectAnthropicProvider, BedrockProvider],
  controllers: [LlmController],
  exports: [LlmService],
})
export class LlmModule {}
