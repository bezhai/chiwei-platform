import { expect, it } from 'bun:test';

for (const scenario of ['redis', 'proxy', 'notification', 'enqueue', 'dedup']) {
    it(`daily discovery continues after ${scenario} failure without marking success`, async () => {
        const process = Bun.spawn([Bun.which('bun')!, `${import.meta.dir}/fixtures/discoveryTimeout.ts`, scenario], {
            stdout: 'pipe', stderr: 'pipe',
        });
        const timer = setTimeout(() => process.kill(), 3000);
        try {
            const [exitCode, stdout, stderr] = await Promise.all([
                process.exited, new Response(process.stdout).text(), new Response(process.stderr).text(),
            ]);
            if (exitCode !== 0) throw new Error(`Scenario ${scenario}: ${stdout}\n${stderr}`);
            expect(stdout).toContain('DISCOVERY_SCENARIO_OK');
        } finally {
            clearTimeout(timer);
        }
    });
}
