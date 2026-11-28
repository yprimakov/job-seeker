import { NextRequest, NextResponse } from 'next/server'
import { spawn } from 'child_process'
import path from 'path'
import fs from 'fs'

export const dynamic = 'force-dynamic'

const rootDir = path.resolve(process.cwd(), '..')
const JOBS_DIR = path.join(rootDir, 'jobs')
const SENTINEL = path.join(JOBS_DIR, '.scraper_running')
const STATUS_PATH = path.join(JOBS_DIR, 'scraper_status.json')
const LOG_PATH = path.join(JOBS_DIR, 'scraper_last_run.log')
const SPEC_PATH = path.join(JOBS_DIR, '.scraper_spec.json')

// A sentinel older than this is left over from a run that was killed
// (window closed, PC rebooted) and no longer means "running".
const STALE_MS = 45 * 60 * 1000

function isRunning(): boolean {
  try {
    const age = Date.now() - fs.statSync(SENTINEL).mtimeMs
    if (age < STALE_MS) return true
    fs.unlinkSync(SENTINEL)
  } catch { /* no sentinel */ }
  return false
}

function readStatus(): unknown {
  try { return JSON.parse(fs.readFileSync(STATUS_PATH, 'utf-8')) } catch { return null }
}

function readLogTail(lines = 40): string {
  try {
    return fs.readFileSync(LOG_PATH, 'utf-8').split(/\r?\n/).slice(-lines).join('\n')
  } catch { return '' }
}

// GET /api/pipeline/scrape — is the scraper running, and how did the last run end
export async function GET() {
  return NextResponse.json({ running: isRunning(), status: readStatus(), log: readLogTail() })
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({})) as {
    queries?: string[]   // multi-title run
    query?: string       // single query (legacy)
    remote?: boolean
    easyApply?: boolean
    pages?: number
  }

  const queries: string[] = []
  if (body.queries?.length) {
    queries.push(...body.queries.map(q => String(q).trim()).filter(Boolean))
  } else if (body.query?.trim()) {
    queries.push(body.query.trim())
  }

  if (queries.length === 0) {
    return NextResponse.json({ ok: false, error: 'At least one search query is required.' }, { status: 400 })
  }

  if (isRunning()) {
    return NextResponse.json({ ok: false, error: 'The scraper is already running.' }, { status: 409 })
  }

  fs.mkdirSync(JOBS_DIR, { recursive: true })
  // Queries go in a JSON file, never on a command line, so titles with
  // characters like & or quotes can't break the launcher.
  fs.writeFileSync(SPEC_PATH, JSON.stringify({
    queries,
    remote: body.remote !== false,
    easyApply: body.easyApply !== false,
    pages: body.pages || undefined,
  }, null, 2))
  try { fs.unlinkSync(STATUS_PATH) } catch { /* none yet */ }
  fs.writeFileSync(SENTINEL, new Date().toISOString())

  const runner = path.join(rootDir, 'pipeline', 'run_scrape.py')

  try {
    if (process.platform === 'win32') {
      // Visible terminal, as before. run_scrape.py writes jobs/scraper_status.json
      // and removes the sentinel however the run ends, and keeps the window open
      // on failure so the error can be read.
      const batPath = path.join(rootDir, '.scraper_launch.bat')
      fs.writeFileSync(batPath, [
        '@echo off',
        `cd /d "${rootDir}"`,
        `python "${runner}" --spec "${SPEC_PATH}"`,
        '',
      ].join('\r\n'))
      const proc = spawn('cmd.exe', ['/c', 'start', '"Job Seeker scraper"', 'cmd', '/c', `"${batPath}"`], {
        detached: true, stdio: 'ignore', shell: false, windowsVerbatimArguments: true,
      })
      proc.on('error', err => failLaunch(err))
      proc.unref()
    } else {
      const proc = spawn('python3', [runner, '--spec', SPEC_PATH, '--no-pause'], {
        cwd: rootDir, detached: true, stdio: 'ignore',
      })
      proc.on('error', err => failLaunch(err))
      proc.unref()
    }
  } catch (err) {
    failLaunch(err as Error)
    return NextResponse.json({ ok: false, error: `Could not start the scraper: ${(err as Error).message}` }, { status: 500 })
  }

  const label = queries.length === 1
    ? `"${queries[0]}"`
    : `${queries.length} queries (${queries.join(', ')})`

  return NextResponse.json({
    ok: true,
    message: `Scraper launched for ${label}. Results will appear here when it finishes.`,
  })
}

// The process never started: record that, so the page doesn't wait forever.
function failLaunch(err: Error) {
  try { fs.unlinkSync(SENTINEL) } catch { /* ok */ }
  try {
    const now = new Date().toISOString()
    fs.writeFileSync(STATUS_PATH, JSON.stringify({
      state: 'failed', startedAt: now, finishedAt: now, queries: [],
      message: `Could not start the scraper: ${err.message}. Is Python on PATH?`,
    }, null, 2))
  } catch { /* ok */ }
}
