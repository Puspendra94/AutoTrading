import { Controller, Get, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { LlmService } from './llm.service';

@Controller('llm')
@UseGuards(AuthGuard('jwt'))
export class LlmController {
  constructor(private readonly llmService: LlmService) {}

  @Get('cost-summary')
  async getCostSummary() {
    return this.llmService.getCostSummary();
  }
}
