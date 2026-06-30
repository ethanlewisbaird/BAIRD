#!/usr/bin/env tsx
/**
 * BAIRD Ink TUI — entry point.
 */

import { render } from 'ink';
import { App } from './components/app.js';

// Disable bracketed paste mode so paste doesn't send escape sequences
process.stdout.write('\x1b[?2004l');

const { waitUntilExit } = render(<App />);

process.on('SIGINT', () => {
  process.exit(0);
});

// Re-enable bracketed paste on exit (good terminal citizen)
process.on('exit', () => {
  process.stdout.write('\x1b[?2004h');
});

await waitUntilExit();
