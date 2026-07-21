import { Injectable, Logger } from '@nestjs/common';
import { Cron, CronExpression } from '@nestjs/schedule';
import { InjectRepository } from '@nestjs/typeorm';
import { MoreThan, Repository } from 'typeorm';
import { Alert, AlertSeverity } from '../entities/alert.entity';
import { NotificationService } from '../modules/notification/notification.service';

@Injectable()
export class AlertDigestJob {
  private readonly logger = new Logger(AlertDigestJob.name);
  private lastDigestAt = new Date();

  constructor(
    @InjectRepository(Alert) private readonly alertRepo: Repository<Alert>,
    private readonly notificationService: NotificationService,
  ) {}

  // Warning-tier alerts batch into a periodic digest rather than notifying individually
  // (spec 4.9/10) — Critical alerts already push immediately from NotificationService.
  @Cron(CronExpression.EVERY_4_HOURS)
  async run() {
    const since = this.lastDigestAt;
    this.lastDigestAt = new Date();

    const warnings = await this.alertRepo.find({
      where: { severity: AlertSeverity.WARNING, createdAt: MoreThan(since) },
    });

    if (warnings.length === 0) return;

    const byCategory = warnings.reduce((acc, a) => {
      acc[a.category] = (acc[a.category] || 0) + 1;
      return acc;
    }, {} as Record<string, number>);

    const summary = Object.entries(byCategory)
      .map(([category, count]) => `${category}: ${count}`)
      .join(', ');

    await this.notificationService.createAlert(
      AlertSeverity.INFO,
      'ALERT_DIGEST',
      `${warnings.length} warning-tier alert(s) in the last digest window — ${summary}`,
    );
    this.logger.log(`Alert digest posted: ${warnings.length} warnings summarized.`);
  }
}
