-- 1. person_impression 加 bot_name 列，存量数据默认 chiwei
ALTER TABLE person_impression ADD COLUMN IF NOT EXISTS bot_name VARCHAR(50) NOT NULL DEFAULT 'chiwei';

-- 删除旧唯一约束，建新约束
ALTER TABLE person_impression DROP CONSTRAINT IF EXISTS person_impression_chat_id_user_id_key;
ALTER TABLE person_impression ADD CONSTRAINT person_impression_chat_id_user_id_bot_name_key
    UNIQUE (chat_id, user_id, bot_name);

-- 2. group_culture_gestalt 从 chat_id 主键改为 id + 唯一约束
-- 先加新列
ALTER TABLE group_culture_gestalt ADD COLUMN IF NOT EXISTS bot_name VARCHAR(50) NOT NULL DEFAULT 'chiwei';
ALTER TABLE group_culture_gestalt ADD COLUMN IF NOT EXISTS id SERIAL;

-- 删除旧主键，建新主键
ALTER TABLE group_culture_gestalt DROP CONSTRAINT IF EXISTS group_culture_gestalt_pkey;
ALTER TABLE group_culture_gestalt ADD PRIMARY KEY (id);
ALTER TABLE group_culture_gestalt ADD CONSTRAINT group_culture_gestalt_chat_id_bot_name_key
    UNIQUE (chat_id, bot_name);
