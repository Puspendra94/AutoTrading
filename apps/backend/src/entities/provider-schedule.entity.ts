import { Entity, PrimaryGeneratedColumn, Column, ManyToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';

@Entity({ name: 'provider_schedule', schema: 'Algo_Trading' })
export class ProviderSchedule {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id' })
  providerId: string;

  @ManyToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column({ name: 'active_days', type: 'simple-array', default: 'Mon,Tue,Wed,Thu,Fri,Sat,Sun' })
  activeDays: string[];

  @Column({ name: 'active_start_time', default: '00:00' })
  activeStartTime: string;

  @Column({ name: 'active_end_time', default: '23:59' })
  activeEndTime: string;

  @Column({ default: 'UTC' })
  timezone: string;
}
