/**
 * Adapter — spawns the Python backend process and bridges JSON events.
 */

import { type ChildProcess, spawn } from 'child_process';
import { createInterface } from 'readline';
import process from 'process';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import type { BackendEvent } from '../types/events.js';

export interface BackendAdapter {
  sendInput(text: string): void;
  sendDialogChoice(choice: string): void;
  kill(): void;
  get alive(): boolean;
}

/** Resolve the BAIRD project root (parent of baird-ink/). */
function projectRoot(): string {
  const dir = dirname(fileURLToPath(import.meta.url));
  return resolve(dir, '../../..');
}

/** Parse BAIRD_PYTHON_CMD env var or default to 'uv run python'. */
function pythonCommand(): [string, string[]] {
  const raw = process.env.BAIRD_PYTHON_CMD || 'uv run python';
  const parts = raw.trim().split(/\s+/);
  return [parts[0] || 'python3', parts.slice(1)];
}

export function startBackend(
  adapterScript: string,
  onEvent: (event: BackendEvent) => void,
  onExit: (code: number | null) => void,
): BackendAdapter {
  const cwd = projectRoot();
  const [cmd, pythonArgs] = pythonCommand();
  const cmdArgs = [...pythonArgs, adapterScript];

  const proc: ChildProcess = spawn(cmd, cmdArgs, {
    cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });

  const rl = createInterface({ input: proc.stdout! });
  rl.on('line', (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      const event: BackendEvent = JSON.parse(trimmed);
      onEvent(event);
    } catch {
      onEvent({ kind: 'status', text: trimmed });
    }
  });

  proc.stderr?.on('data', (data: Buffer) => {
    const text = data.toString().trim();
    if (text) {
      onEvent({ kind: 'status', text });
    }
  });

  proc.on('exit', (code: number | null) => {
    onExit(code);
  });

  let dead = false;

  return {
    sendInput(text: string) {
      if (!dead && proc.stdin?.writable) {
        proc.stdin.write(JSON.stringify({ command: 'input', text }) + '\n');
      }
    },
    sendDialogChoice(choice: string) {
      if (!dead && proc.stdin?.writable) {
        proc.stdin.write(JSON.stringify({ command: 'dialog', choice }) + '\n');
      }
    },
    kill() {
      proc.kill();
      dead = true;
    },
    get alive() { return !dead; },
  };
}
