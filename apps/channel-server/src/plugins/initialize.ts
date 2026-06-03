import { initializeChannelRuntimes } from './runtime';

export async function initializeChannelPlugins(): Promise<void> {
    await initializeChannelRuntimes();
}
