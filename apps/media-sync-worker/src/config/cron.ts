import cron from 'node-cron';

export const DEFAULT_DOWNLOAD_CRON = '12 10 * * *';

export function loadDownloadCron(env: NodeJS.ProcessEnv = process.env): string {
  const cronTime = env.DOWNLOAD_CRON?.trim() || DEFAULT_DOWNLOAD_CRON;

  if (!cron.validate(cronTime)) {
    throw new Error(`Invalid DOWNLOAD_CRON: ${cronTime}`);
  }

  return cronTime;
}
