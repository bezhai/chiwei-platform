import { Column, Entity, PrimaryGeneratedColumn } from 'typeorm';

@Entity('diary_entry')
export class DiaryEntry {
  @PrimaryGeneratedColumn()
  id!: number;

  @Column({ length: 100 })
  chat_id!: string;

  @Column({ length: 10 })
  diary_date!: string;

  @Column('text')
  content!: string;

  @Column({ type: 'integer', default: 0 })
  message_count!: number;

  @Column({ length: 100, nullable: true })
  model!: string | null;

  @Column({ type: 'timestamptz', default: () => 'NOW()' })
  created_at!: Date;
}
