import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';

export enum ReconciliationStatus {
  CLEAN = 'clean',
  MISMATCH = 'mismatch',
}

@Entity({ name: 'reconciliation_reports', schema: 'Algo_Trading' })
export class ReconciliationReport {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id' })
  providerId: string;

  @ManyToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @CreateDateColumn({ name: 'run_at' })
  runAt: Date;

  @Column({ name: 'mismatches_found', type: 'int', default: 0 })
  mismatchesFound: number;

  @Column({ name: 'detail_json', type: 'jsonb', nullable: true })
  detailJson: Record<string, any>;

  @Column({ type: 'enum', enum: ReconciliationStatus, default: ReconciliationStatus.CLEAN })
  status: ReconciliationStatus;
}
