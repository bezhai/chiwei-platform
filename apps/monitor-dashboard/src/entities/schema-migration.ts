import { Column, Entity, PrimaryGeneratedColumn } from 'typeorm';

@Entity('schema_migrations')
export class SchemaMigration {
  @PrimaryGeneratedColumn()
  id!: number;

  @Column({ length: 20, unique: true })
  version!: string;

  @Column({ length: 200 })
  name!: string;

  @Column('text')
  sql_content!: string;

  @Column({ type: 'timestamptz', default: () => 'NOW()' })
  applied_at!: Date;

  @Column({ length: 100, default: 'manual' })
  applied_by!: string;

  @Column({ length: 20 })
  status!: string;

  @Column({ type: 'text', nullable: true })
  error_message!: string | null;

  @Column({ type: 'integer', nullable: true })
  duration_ms!: number | null;
}
