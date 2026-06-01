import {
  Column,
  CreateDateColumn,
  Entity,
  PrimaryColumn,
  Unique,
  UpdateDateColumn,
} from "typeorm";

export interface CommonAgentResponseReply {
  common_message_id: string;
  content_type?: string;
  sent_at: string;
}

export interface CommonSafetyResult {
  reason?: string;
  detail?: string;
  confidence?: number;
  recalled?: number;
  failed?: number;
  checked_at?: string;
}

@Entity("common_agent_response")
@Unique("uq_common_agent_response_session", ["session_id"])
export class CommonAgentResponse {
  @PrimaryColumn({ type: "uuid" })
  response_id!: string;

  @Column({ type: "varchar", length: 100 })
  session_id!: string;

  @Column({ type: "uuid" })
  trigger_common_message_id!: string;

  @Column({ type: "uuid" })
  common_conversation_id!: string;

  @Column({ type: "varchar", length: 50, nullable: true })
  bot_name?: string;

  @Column({ type: "varchar", length: 50, nullable: true })
  persona_id?: string;

  @Column({ type: "varchar", length: 30, default: "reply" })
  response_type!: string;

  @Column({ type: "jsonb", default: () => "'[]'::jsonb" })
  replies!: CommonAgentResponseReply[];

  @Column({ type: "text", nullable: true })
  response_text?: string;

  @Column({ type: "jsonb", default: () => "'{}'::jsonb" })
  agent_metadata!: Record<string, unknown>;

  @Column({ type: "varchar", length: 20, default: "pending" })
  safety_status!: string;

  @Column({ type: "jsonb", nullable: true })
  safety_result?: CommonSafetyResult;

  @Column({ type: "varchar", length: 20, default: "pending" })
  status!: string;

  @CreateDateColumn({ name: "created_at", type: "timestamptz" })
  created_at!: Date;

  @UpdateDateColumn({ name: "updated_at", type: "timestamptz" })
  updated_at!: Date;
}
