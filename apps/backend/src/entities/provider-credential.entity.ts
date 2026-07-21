import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, UpdateDateColumn, OneToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';

@Entity({ name: 'provider_credentials', schema: 'Algo_Trading' })
export class ProviderCredential {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id', unique: true })
  providerId: string;

  @OneToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column({ name: 'credential_json', type: 'jsonb' })
  credentialJson: Record<string, any>;

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date;

  @UpdateDateColumn({ name: 'updated_at' })
  updatedAt: Date;
}
