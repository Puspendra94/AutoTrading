import { Controller, Get, Patch, Param, Query, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { NotificationService } from './notification.service';
import { AlertSeverity } from '../../entities/alert.entity';

@Controller('alerts')
@UseGuards(AuthGuard('jwt'))
export class NotificationController {
  constructor(private readonly notificationService: NotificationService) {}

  @Get()
  async listAlerts(@Query('severity') severity?: AlertSeverity) {
    return this.notificationService.listAlerts(severity);
  }

  @Patch(':id/ack')
  async acknowledgeAlert(@Param('id') id: string) {
    return this.notificationService.acknowledgeAlert(id);
  }
}
