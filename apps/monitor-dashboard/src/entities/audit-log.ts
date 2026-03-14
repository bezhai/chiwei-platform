import { Column, Entity, PrimaryGeneratedColumn } from 'typeorm';

@Entity('audit_logs')
export class AuditLog {
  @PrimaryGeneratedColumn()
  id!: number;

  @Column({ length: 20 })
  caller!: string;

  @Column({ length: 100 })
  action!: string;

  @Column({ type: 'jsonb', nullable: true })
  params!: Record<string, unknown> | null;

  @Column({ length: 20 })
  result!: string;

  @Column({ type: 'text', nullable: true })
  error_message!: string | null;

  @Column({ type: 'integer', nullable: true })
  duration_ms!: number | null;

  @Column({ type: 'timestamptz', default: () => 'NOW()' })
  created_at!: Date;
}
