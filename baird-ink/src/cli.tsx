#!/usr/bin/env tsx
/**
 * BAIRD Ink TUI — entry point.
 *
 * Spawns the Python backend process and renders the Ink app.
 */

import { render } from 'ink';
import { App } from './components/app.js';

const { waitUntilExit } = render(<App />);

// Exit on Ctrl+C / SIGINT
process.on('SIGINT', () => {
  process.exit(0);
});

await waitUntilExit();
