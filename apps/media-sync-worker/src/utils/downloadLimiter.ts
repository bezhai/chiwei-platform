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
    if (limit <= 0) {
      throw new Error("limit must be greater than 0");
    }
    if (tasks.length === 0) {
      return [];
    }

    const results = new Array<T>(tasks.length);
    let nextIndex = 0;
    const workerCount = Math.min(limit, tasks.length);

    async function worker(): Promise<void> {
      while (true) {
        const index = nextIndex;
        nextIndex++;
        if (index >= tasks.length) {
          return;
        }
        results[index] = await tasks[index]();
      }
    }

    await Promise.all(Array.from({ length: workerCount }, () => worker()));
    return results;
  }


export { DownloadLimiter, limitConcurrency};
