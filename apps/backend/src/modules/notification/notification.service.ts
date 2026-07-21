import { Inject, Injectable, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import Redis from 'ioredis';
import { Alert, AlertSeverity } from '../../entities/alert.entity';
import { REDIS_PUBLISHER } from '../../common/redis/redis.module';
import { ALERTS_CHANNEL } from '../../websockets/redis-alerts-bridge.service';

@Injectable()
export class NotificationService {
  private readonly logger = new Logger(NotificationService.name);

  constructor(
    @InjectRepository(Alert)
    private readonly alertRepo: Repository<Alert>,
    @Inject(REDIS_PUBLISHER) private readonly redisPublisher: Redis,
  ) {}

  async createAlert(
    severity: AlertSeverity,
    category: string,
    message: string,
    entityType?: string,
    entityId?: string,
  ) {
    const alert = this.alertRepo.create({
      severity,
      category,
      message,
      relatedEntityType: entityType,
      relatedEntityId: entityId,
    });
    await this.alertRepo.save(alert);

    // Cross-process live delivery: works whether this was called from the API process
    // (which also has a local TradingGateway) or the worker (which doesn't) — the API
    // process's RedisAlertsBridgeService is the only subscriber and forwards to sockets.
    this.redisPublisher.publish(ALERTS_CHANNEL, JSON.stringify(alert)).catch((err) =>
      this.logger.warn(`Failed to publish alert to Redis: ${err.message}`),
    );

    if (severity === AlertSeverity.CRITICAL) {
      await this.sendTelegramPush(alert).catch((err) =>
        this.logger.warn(`Telegram push failed (non-fatal): ${err.message}`),
      );
    }

    return alert;
  }

  private async sendTelegramPush(alert: Alert) {
    const token = process.env.TELEGRAM_BOT_TOKEN;
    const chatId = process.env.TELEGRAM_CHAT_ID;
    if (!token || !chatId) {
      this.logger.debug('TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — critical alert recorded in DB only.');
      return;
    }
    const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        text: `🚨 CRITICAL [${alert.category}]\n${alert.message}`,
      }),
    });
    if (!res.ok) {
      throw new Error(`Telegram API returned HTTP ${res.status}`);
    }
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
