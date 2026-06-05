import { describe, expect, it } from 'bun:test';

import { DEFAULT_DOWNLOAD_CRON, loadDownloadCron } from '../src/config/cron';

describe('loadDownloadCron', () => {
  it('defaults the download task to 10:12 every day', () => {
    expect(loadDownloadCron({} as NodeJS.ProcessEnv)).toBe(DEFAULT_DOWNLOAD_CRON);
    expect(DEFAULT_DOWNLOAD_CRON).toBe('12 10 * * *');
  });

  it('uses DOWNLOAD_CRON when configured', () => {
    expect(loadDownloadCron({ DOWNLOAD_CRON: '15 8 * * *' } as NodeJS.ProcessEnv)).toBe('15 8 * * *');
  });

  it('rejects invalid cron expressions', () => {
    expect(() => loadDownloadCron({ DOWNLOAD_CRON: 'not-a-cron' } as NodeJS.ProcessEnv)).toThrow(
      'Invalid DOWNLOAD_CRON: not-a-cron',
    );
  });
});
