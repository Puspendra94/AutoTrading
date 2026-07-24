import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, UpdateDateColumn, OneToMany } from 'typeorm';
import { Provider } from './provider.entity';

@Entity({ name: 'users', schema: 'Algo_Trading' })
export class User {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ unique: true })
  email: string;

  @Column({ name: 'password_hash' })
  passwordHash: string;

  // Password reset (first-party, no third-party auth). Stores the SHA-256 hash of a
  // single-use reset token (deterministic so the reset endpoint can look the row up by
  // the raw token alone) plus its expiry. Both cleared once the token is consumed.
  @Column({ name: 'password_reset_token_hash', type: 'text', nullable: true })
  passwordResetTokenHash: string | null;

  @Column({ name: 'password_reset_expires_at', type: 'timestamptz', nullable: true })
  passwordResetExpiresAt: Date | null;

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date;

  @UpdateDateColumn({ name: 'updated_at' })
  updatedAt: Date;

  @OneToMany(() => Provider, (provider) => provider.user)
  providers: Provider[];
}
