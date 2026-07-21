import {
  WebSocketGateway,
  WebSocketServer,
  SubscribeMessage,
  OnGatewayConnection,
  OnGatewayDisconnect,
  MessageBody,
  ConnectedSocket,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { Injectable } from '@nestjs/common';
import { ExecutionService } from '../modules/risk-execution/execution.service';

@WebSocketGateway({
  cors: {
    origin: '*',
  },
})
@Injectable()
export class TradingGateway implements OnGatewayConnection, OnGatewayDisconnect {
  @WebSocketServer()
  server: Server;

  constructor(private readonly executionService: ExecutionService) {}

  handleConnection(client: Socket) {
    console.log(`Client connected: ${client.id}`);
    this.sendOpenPositionsStream(client);
  }

  handleDisconnect(client: Socket) {
    console.log(`Client disconnected: ${client.id}`);
  }

  @SubscribeMessage('subscribe_ticker')
  handleSubscribeTicker(@MessageBody() data: { tickerId: string }, @ConnectedSocket() client: Socket) {
    client.join(`ticker_${data.tickerId}`);
    return { event: 'subscribed', tickerId: data.tickerId };
  }

  @SubscribeMessage('unsubscribe_ticker')
  handleUnsubscribeTicker(@MessageBody() data: { tickerId: string }, @ConnectedSocket() client: Socket) {
    client.leave(`ticker_${data.tickerId}`);
    return { event: 'unsubscribed', tickerId: data.tickerId };
  }

  broadcastPriceUpdate(tickerId: string, candle: any) {
    this.server.to(`ticker_${tickerId}`).emit('price_update', { tickerId, candle });
  }

  async broadcastPositionUpdate() {
    const positions = await this.executionService.getOpenPositions();
    this.server.emit('live_positions_update', positions);
  }

  private async sendOpenPositionsStream(client: Socket) {
    const positions = await this.executionService.getOpenPositions();
    client.emit('live_positions_update', positions);
  }
}
