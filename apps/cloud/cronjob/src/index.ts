import * as dotenv from 'dotenv';  // 导入 dotenv
dotenv.config();

import cron from 'node-cron';  // 导入 node-cron
import { startDownload } from './service/dailyDownload';
import { consumeDownloadTaskAsync } from './service/consumeService';
import { mongoInitPromise } from './mongo/client';
import { syncBangumiArchive } from './service/bangumiArchiveService';

// 重试配置
const RETRY_DELAYS = [1000, 5000, 15000]; // 重试延迟时间（毫秒）

// 延迟函数
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

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

// 定时任务：下载任务
scheduleTask('0 10 * * *', 'download task', startDownload);

// 定时任务：Bangumi Archive 数据同步 (每周三上午7点)
scheduleTask('0 7 * * 3', 'bangumi archive sync', syncBangumiArchive);
// 异步消费任务
(async () => {
  await mongoInitPromise;  // 等待 MongoDB 初始化完成
  
  try {
    await consumeDownloadTaskAsync();  // 启动异步任务的消费逻辑
  } catch (err) {
    console.error('Error in the consume download task:', err);
  }
})();

