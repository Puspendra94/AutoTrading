import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Alert, AlertSeverity } from '../../entities/alert.entity';

@Injectable()
export class NotificationService {
  constructor(
    @InjectRepository(Alert)
    private readonly alertRepo: Repository<Alert>,
  ) {}

  async createAlert(severity: AlertSeverity, category: string, message: string, entityType?: string, entityId?: string) {
    const alert = this.alertRepo.create({
      severity,
      category,
      message,
      relatedEntityType: entityType,
      relatedEntityId: entityId,
    });
    return this.alertRepo.save(alert);
  }

  async listAlerts(severity?: AlertSeverity) {
    if (severity) {
      return this.alertRepo.find({ where: { severity }, order: { createdAt: 'DESC' }, take: 50 });
    }
    return this.alertRepo.find({ order: { createdAt: 'DESC' }, take: 50 });
  }

  async acknowledgeAlert(id: string) {
    const alert = await this.alertRepo.findOne({ where: { id } });
    if (alert) {
      alert.acknowledgedAt = new Date();
      await this.alertRepo.save(alert);
    }
    return alert;
  }
}
