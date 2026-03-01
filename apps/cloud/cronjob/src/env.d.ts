declare namespace NodeJS {
  interface ProcessEnv {
    REDIS_PASSWORD: string;
    APP_ID: string;
    APP_SECRET: string;
    MONGO_INITDB_ROOT_USERNAME: string;
    MONGO_INITDB_ROOT_PASSWORD: string;
    SELF_CHAT_ID: string;
    HTTP_SECRET: string;
    REDIS_HOST: string;
    BANGUMI_ACCESS_TOKEN: string;
    MONGO_HOST: string;
  }
}
