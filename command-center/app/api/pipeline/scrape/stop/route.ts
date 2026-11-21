import { NextResponse } from 'next/server'
import { spawn } from 'child_process'
import path from 'path'
import fs from 'fs'

export const dynamic = 'force-dynamic'

const rootDir = path.resolve(process.cwd(), '..')
const SENTINEL = path.join(rootDir, 'jobs', '.scraper_running')
const STATUS_PATH = path.join(rootDir, 'jobs', 'scraper_status.json')

// POST /api/pipeline/scrape/stop
// Kills the scrape runner and any scraper it started, then records the stop
// so the Jobs page (which watches scraper_status.json) leaves its running state.
export async function POST() {
  await new Promise<void>(resolve => {
    const proc = process.platform === 'win32'
      ? spawn(
          'powershell',
          [
            '-NonInteractive', '-NoProfile', '-Command',
            `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*linkedin_scraper*' -or $_.CommandLine -like '*run_scrape.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`,
          ],
          { shell: false },
        )
      : spawn('pkill', ['-f', 'run_scrape.py|linkedin_scraper.py'], { shell: false })
    proc.on('close', () => resolve())
    proc.on('error', () => resolve())
  })

  try { fs.unlinkSync(SENTINEL) } catch { /* already gone */ }
  try {
    const now = new Date().toISOString()
    fs.writeFileSync(STATUS_PATH, JSON.stringify({
      state: 'stopped', startedAt: now, finishedAt: now, queries: [], message: 'Scraper stopped.',
    }, null, 2))
  } catch { /* ok */ }

  return NextResponse.json({ ok: true })
}
