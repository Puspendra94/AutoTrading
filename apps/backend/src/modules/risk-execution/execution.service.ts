import { Injectable, BadRequestException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Position, PositionStatus, PositionSide } from '../../entities/position.entity';
import { Order, OrderSide, OrderType, OrderStatus } from '../../entities/order.entity';
import { RiskGateService } from './risk-gate.service';

@Injectable()
export class ExecutionService {
  constructor(
    @InjectRepository(Position)
    private readonly positionRepo: Repository<Position>,
    @InjectRepository(Order)
    private readonly orderRepo: Repository<Order>,
    private readonly riskGateService: RiskGateService,
  ) {}

  async executeTradeSignal(tickerId: string, side: PositionSide, price: number, strategyId?: string) {
    // 1. Pass through Risk Gate
    const riskCheck = await this.riskGateService.evaluateOrderRiskGate({
      tickerId,
      strategyId,
      side,
      price,
    });

    if (!riskCheck.approved) {
      return { status: 'REJECTED', reason: riskCheck.reason };
    }

    // 2. Open Position record
    const position = this.positionRepo.create({
      tickerId,
      strategyId,
      side,
      status: PositionStatus.OPEN,
      entryPrice: price,
      currentPrice: price,
      quantity: riskCheck.allowedQuantity,
      unrealizedPl: 0,
      realizedPl: 0,
      isProbation: riskCheck.isProbation,
    });
    await this.positionRepo.save(position);

    // 3. Create & Fill Order record
    const order = this.orderRepo.create({
      positionId: position.id,
      providerOrderId: `ord_${Date.now()}`,
      side: side === PositionSide.LONG ? OrderSide.BUY : OrderSide.SELL,
      quantity: riskCheck.allowedQuantity,
      price,
      orderType: OrderType.MARKET,
      status: OrderStatus.FILLED,
      filledAt: new Date(),
    });
    await this.orderRepo.save(order);

    return { status: 'EXECUTED', position, order };
  }

  async closePosition(positionId: string, exitPrice: number) {
    const position = await this.positionRepo.findOne({ where: { id: positionId } });
    if (!position || position.status === PositionStatus.CLOSED) {
      throw new BadRequestException('Position not found or already closed');
    }

    const priceDiff = exitPrice - Number(position.entryPrice);
    const realizedPl = Number((priceDiff * Number(position.quantity)).toFixed(4));

    position.status = PositionStatus.CLOSED;
    position.exitPrice = exitPrice;
    position.currentPrice = exitPrice;
    position.realizedPl = realizedPl;
    position.unrealizedPl = 0;
    position.closedAt = new Date();
    await this.positionRepo.save(position);

    const closeOrder = this.orderRepo.create({
      positionId: position.id,
      providerOrderId: `ord_exit_${Date.now()}`,
      side: position.side === PositionSide.LONG ? OrderSide.SELL : OrderSide.BUY,
      quantity: Number(position.quantity),
      price: exitPrice,
      orderType: OrderType.MARKET,
      status: OrderStatus.FILLED,
      filledAt: new Date(),
    });
    await this.orderRepo.save(closeOrder);

    return { position, closeOrder };
  }

  async getOpenPositions() {
    return this.positionRepo.find({
      where: { status: PositionStatus.OPEN },
      relations: ['ticker', 'strategy'],
      order: { openedAt: 'DESC' },
    });
  }

  async getAllPositions() {
    return this.positionRepo.find({
      relations: ['ticker', 'strategy'],
      order: { openedAt: 'DESC' },
      take: 100,
    });
  }
}
