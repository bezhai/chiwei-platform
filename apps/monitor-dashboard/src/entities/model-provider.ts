import {
  Column,
  Entity,
  PrimaryColumn,
  CreateDateColumn,
  UpdateDateColumn,
} from 'typeorm';

@Entity('model_provider')
export class ModelProvider {
  @PrimaryColumn({ type: 'uuid' })
  provider_id!: string;

  @Column({ type: 'varchar', length: 100 })
  name!: string;

  @Column({ type: 'text' })
  api_key!: string;

  @Column({ type: 'text' })
  base_url!: string;

  @Column({ type: 'varchar', length: 50, default: 'openai' })
  client_type!: string;

  @Column({ type: 'boolean', default: true })
  is_active!: boolean;

  @CreateDateColumn({ type: 'timestamp' })
  created_at!: Date;

  @UpdateDateColumn({ type: 'timestamp' })
  updated_at!: Date;
}
