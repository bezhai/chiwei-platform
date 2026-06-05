declare namespace NodeJS {
  interface ProcessEnv {
    REDIS_PASSWORD: string;
    APP_ID: string;
    APP_SECRET: string;
    MONGO_INITDB_ROOT_USERNAME: string;
    MONGO_INITDB_ROOT_PASSWORD: string;
    SELF_CHAT_ID: string;
    HTTP_SECRET: string;
    PROXY_HTTP_SECRET: string;
    REDIS_HOST: string;
    REDIS_PORT: string;
    BANGUMI_ACCESS_TOKEN: string;
    MONGO_HOST: string;
    MONGO_PORT: string;
    MONGO_CONNECT_TIMEOUT_MS: string;
    DOWNLOAD_CRON: string;
    RUN_CONNECTIVITY_CHECK: string;
    DISABLE_SCHEDULES: string;
    DISABLE_CONSUMER: string;
    END_POINT: string;
    OSS_ACCESS_KEY_ID: string;
    OSS_ACCESS_KEY_SECRET: string;
    OSS_BUCKET: string;
    MINIO_ENDPOINT: string;
    MINIO_PORT: string;
    MINIO_USE_SSL: string;
    MINIO_ACCESS_KEY: string;
    MINIO_SECRET_KEY: string;
    MINIO_BUCKET: string;
    MINIO_SYNC_ENABLED: string;
  }
}
