import { Column, Entity, PrimaryGeneratedColumn } from 'typeorm';

@Entity('weekly_review')
export class WeeklyReview {
  @PrimaryGeneratedColumn()
  id!: number;

  @Column({ length: 100 })
  chat_id!: string;

  @Column({ length: 10 })
  week_start!: string;

  @Column({ length: 10 })
  week_end!: string;

  @Column('text')
  content!: string;

  @Column({ length: 100, nullable: true })
  model!: string | null;

  @Column({ type: 'timestamptz', default: () => 'NOW()' })
  created_at!: Date;
}
