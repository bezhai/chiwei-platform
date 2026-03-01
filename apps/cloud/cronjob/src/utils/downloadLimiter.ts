class DownloadLimiter {
  maxDownloads: number;
  downloadCounter: number;
  cooldown: number;
  coolingDown: boolean;

  constructor(maxDownloads: number, cooldown: number) {
    this.maxDownloads = maxDownloads; // 最大下载次数
    this.downloadCounter = 0; // 已下载计数
    this.cooldown = cooldown; // 冷却时间（以毫秒为单位）
    this.coolingDown = false; // 是否在冷却期
  }

  async tryDownload() {
    // 增加下载计数
    this.downloadCounter++;
    console.log(`Downloaded ${this.downloadCounter} times`);

    // 如果下载次数达到限制，进入冷却期
    if (this.downloadCounter >= this.maxDownloads) {
      console.log(
        `DownloadLimiter is cooling down for ${this.cooldown / 1000} seconds...`
      );
      await this.cooldownPeriod(); // 进入冷却期
      console.log(`DownloadLimiter cooling down finished.`);

      // 重置下载计数器
      this.downloadCounter = 0;
    }
  }

  // 模拟冷却期
  private async cooldownPeriod() {
    this.coolingDown = true;
    return new Promise<void>((resolve) => {
      setTimeout(() => {
        this.coolingDown = false;
        resolve(); // 冷却期结束
      }, this.cooldown);
    });
  }
}

async function limitConcurrency<T>(limit: number, tasks: (() => Promise<T>)[]): Promise<T[]> {
    const results: T[] = [];
    const executing: Promise<void>[] = [];
  
    for (const task of tasks) {
      const p = task().then((result) => {
        results.push(result);  // 将结果添加到结果数组中
      });
  
      // 将执行中的任务推入执行队列
      executing.push(p.then(() => undefined));  // 确保 Promise<void> 类型
  
      // 控制并发数量
      if (executing.length >= limit) {
        await Promise.race(executing);  // 等待队列中的某个任务完成
        // 移除已完成的任务
        executing.splice(executing.findIndex((e) => e === p), 1);
      }
    }
  
    await Promise.all(executing);  // 等待所有剩余的任务完成
    return results;
  }


export { DownloadLimiter, limitConcurrency};
