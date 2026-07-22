import * as dotenv from 'dotenv';  // 导入 dotenv
dotenv.config();

import cron from 'node-cron';  // 导入 node-cron
import { loadDownloadCron } from './config/cron';
import { initTaggerRuntime } from './tagger/runtime';
import { initPostDownloadReconcileRuntime } from './service/postDownloadReconcileRuntime';

// 重试配置
const RETRY_DELAYS = [1000, 5000, 15000]; // 重试延迟时间（毫秒）

// 延迟函数
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

const isEnabled = (value: string | undefined): boolean => {
  return value === '1' || value?.toLowerCase() === 'true';
};

const waitForever = () => {
  setInterval(() => {
    // Keep the worker process alive when all business loops are disabled.
  }, 60 * 60 * 1000);
};

// 抽象定时任务启动函数
const scheduleTask = (cronTime: string, taskName: string, taskFn: () => Promise<any> | any) => {
  const task = cron.schedule(cronTime, async () => {
    console.log(`Starting ${taskName}...`);
    
    for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt++) {
      try {
        await Promise.resolve(taskFn()); // 确保能处理同步和异步函数
        console.log(`Successfully completed ${taskName}`);
        break; // 成功执行，跳出重试循环
      } catch (err) {
        const retryDelay = RETRY_DELAYS[attempt];
        
        if (retryDelay === undefined) {
          // 已经用完所有重试次数
          console.error(`Final error in ${taskName} after ${RETRY_DELAYS.length} retries:`, err);
          // 这里可以添加告警通知逻辑
          break;
        }

        console.error(`Error in ${taskName} (attempt ${attempt + 1}/${RETRY_DELAYS.length + 1}):`, err);
        console.log(`Retrying ${taskName} in ${retryDelay}ms...`);
        await delay(retryDelay);
      }
    }
  });

  task.start();
  console.log(`Cron job scheduled for ${taskName} at ${cronTime}.`);
};

const disableSchedules = isEnabled(process.env.DISABLE_SCHEDULES);
const disableConsumer = isEnabled(process.env.DISABLE_CONSUMER);
const runConnectivityCheck = isEnabled(process.env.RUN_CONNECTIVITY_CHECK);
const needsSourceMongoAtStartup = !disableConsumer
  || isEnabled(process.env.TAGGER_TRIGGER_ENABLED)
  || isEnabled(process.env.POST_DOWNLOAD_RECONCILE_ENABLED);

async function initBackgroundRuntime(): Promise<void> {
  // Trigger/reconcile workers can call ImgCollection immediately. Source Mongo must
  // therefore be ready before either worker loop is allowed to start.
  if (needsSourceMongoAtStartup) {
    const { mongoInitPromise } = await import('./mongo/client');
    await mongoInitPromise;
  }
  await initTaggerRuntime();
  await initPostDownloadReconcileRuntime();
}

const backgroundRuntimeInitPromise = initBackgroundRuntime().catch((err) => {
  console.error('Background runtime initialization failed:', err);
  process.exit(1);
});

async function checkDataConnections() {
  console.log('Checking MongoDB and Redis connectivity...');

  const [{ mongoInitPromise }, { default: redisClient }] = await Promise.all([
    import('./mongo/client'),
    import('./redis/redisClient'),
  ]);

  await mongoInitPromise;
  await redisClient.getNativeClient().ping();
  await redisClient.close();

  console.log('MongoDB and Redis connectivity check completed.');

  console.log('Checking OSS and MinIO connectivity...');
  const { checkStorageConnectivity } = await import('./storage/connectivity');
  const storage = await checkStorageConnectivity();
  console.log(
    `Storage connectivity check completed: OSS=${storage.oss ? 'OK' : 'FAILED'}, MinIO=${storage.minio ? 'OK' : 'FAILED'}.`,
  );
}

if (disableSchedules) {
  console.log('Cron schedules disabled by DISABLE_SCHEDULES.');
} else {
  const downloadCron = loadDownloadCron();

  // 定时任务：下载任务
  scheduleTask(downloadCron, 'download task', async () => {
    const { startDownload } = await import('./service/dailyDownload');
    await startDownload();
  });

  // 定时任务：Bangumi Archive 数据同步 (每周三上午7点)
  scheduleTask('0 7 * * 3', 'bangumi archive sync', async () => {
    const { syncBangumiArchive } = await import('./service/bangumiArchiveService');
    await syncBangumiArchive();
  });
}

// 异步消费任务
(async () => {
  if (disableConsumer) {
    console.log('Download task consumer disabled by DISABLE_CONSUMER.');
    if (disableSchedules) {
      if (runConnectivityCheck) {
        await checkDataConnections();
      }
      console.log('All cronjob work is disabled; keeping process alive for deployment validation.');
      waitForever();
    }
    return;
  }

  try {
    const { consumeDownloadTaskAsync } = await import('./service/consumeService');
    await backgroundRuntimeInitPromise;
    await consumeDownloadTaskAsync();  // 启动异步任务的消费逻辑
  } catch (err) {
    console.error('Error in the consume download task:', err);
    // A worker with a dead consumer but live cron/Tagger timers looks healthy to
    // Kubernetes and never recovers. Exit so the original supervisor restarts it.
    process.exit(1);
  }
})();
