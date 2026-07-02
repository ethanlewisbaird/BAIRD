import { useState, useRef, useEffect } from 'react';
import { Box, Text, useInput } from 'ink';
import { Header } from './header.js';
import { MessageViewport } from './message-viewport.js';
import { StatusBar } from './status-bar.js';
import { InputBar, COMMANDS, matching } from './input-bar.js';
import { Dialog } from './dialog.js';
import { useSessionStore, useUIStore } from '../store/index.js';
import { startBackend, type BackendAdapter } from '../adapter/backend.js';
import { SPINNER_INTERVAL, colors } from '../theme.js';
import type { BackendEvent } from '../types/events.js';

export function App() {
  const lastError = useSessionStore((s) => s.lastError);
  const dialog = useUIStore((s) => s.dialog);

  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef(inputValue);
  inputRef.current = inputValue;

  // Message history for up/down arrow recall
  const [history, setHistory] = useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const savedInput = useRef('');

  // Slash command suggestions
  const showSuggestions = inputValue.startsWith('/') && inputValue.length > 1;
  const suggestions = showSuggestions ? matching(COMMANDS, inputValue.slice(1)) : [];
  const [selectedSuggestion, setSelectedSuggestion] = useState(0);

  // Reset selection when suggestion list shrinks below current index
  useEffect(() => {
    if (selectedSuggestion >= suggestions.length) {
      setSelectedSuggestion(Math.max(0, suggestions.length - 1));
    }
  }, [suggestions.length]);
  const clampedSelected = Math.min(selectedSuggestion, Math.max(0, suggestions.length - 1));

  const adapterRef = useRef<BackendAdapter | null>(null);

  /**
 * Read one line from stdin in cooked mode, temporarily detaching Ink's
 * stdin listener so pasted text goes only to us.
 */
function readLineCooked(): Promise<string> {
  return new Promise((resolve) => {
    const { stdin } = process;
    // Find Ink's internal data listener so we can detach it temporarily
    const inkListeners = stdin.listeners('data') as ((chunk: Buffer) => void)[];
    for (const fn of inkListeners) {
      stdin.removeListener('data', fn);
    }

    const wasRaw = stdin.isRaw;
    if (stdin.setRawMode) {
      stdin.setRawMode(false);
    }
    stdin.resume();

    let buf = '';
    const onData = (chunk: Buffer) => {
      buf += chunk.toString('utf-8');
      if (buf.includes('\n') || buf.includes('\r')) {
        if (stdin.setRawMode && wasRaw) {
          stdin.setRawMode(true);
        }
        stdin.removeListener('data', onData);
        // Re-attach Ink's listeners
        for (const fn of inkListeners) {
          stdin.on('data', fn);
        }
        resolve(buf.replace(/\r?\n$/, ''));
      }
    };
    stdin.on('data', onData);
  });
}

// ── Backend adapter lifecycle ──
  useEffect(() => {
    const onEvent = (event: BackendEvent) => {
      if (event.kind === 'dialog') {
        useUIStore.getState().setDialog({
          kind: 'info',
          title: event.title,
          body: event.body,
          choices: event.choices,
          result: null,
        });
        return;
      }
      if (event.kind === 'status' && event.text) {
        // Show status output as messages in the chat
        const ss = useSessionStore.getState();
        ss.dispatchEvent({ kind: 'user_message', text: event.text } as never);
        return;
      }
      useSessionStore.getState().dispatchEvent(event);
    };
    const onExit = (_code: number | null) => {
      if (_code === 0) {
        process.exit(0);
        return;
      }
      useSessionStore.getState().dispatchEvent({
        kind: 'error',
        text: `backend exited (code ${_code}) — press Ctrl+C to quit`,
      } as any);
    };
    const adapter = startBackend(
      'baird-ink/backend/adapter.py',
      onEvent,
      onExit,
    );
    adapterRef.current = adapter;
    return () => { adapter.kill(); };
  }, []);

  // ── Spinner tick — start/stop based on running tools ──
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;

    const unsub = useSessionStore.subscribe((state) => {
      const running = state.messages.some((m) =>
        m.parts.some((p) => p.kind === 'tool_invocation' && p.state === 'running')
      );
      if (running && !id) {
        id = setInterval(() => {
          useUIStore.getState().tickSpinner();
        }, SPINNER_INTERVAL);
      } else if (!running && id) {
        clearInterval(id);
        id = null;
      }
    });

    return () => {
      unsub();
      if (id) clearInterval(id);
    };
  }, []);

  // ── Centralized input handler ──
  useInput((_input, key) => {
    // ── Dialog mode (takes priority so paste works) ──
    if (dialog) {
      if (key.ctrl && _input === 'c') { process.exit(0); return; }
      // Choices-based dialog (Escape dismisses)
      if (dialog.choices.length > 0) {
        if (key.escape) { useUIStore.getState().setDialog(null); return; }
        if (_input >= '1' && _input <= '9') {
          const idx = parseInt(_input, 10) - 1;
          if (idx < dialog.choices.length) {
            adapterRef.current?.sendDialogChoice(_input);
            useUIStore.getState().setDialog(null);
          }
          return;
        }
        if (_input === 'q' || _input === 'y' || _input === 'n') {
          adapterRef.current?.sendDialogChoice(_input);
          useUIStore.getState().setDialog(null);
          return;
        }
        return;
      }
      // Text-input dialog
      if (key.ctrl && _input === 'c') { process.exit(0); return; }
      if (key.ctrl && _input === 'p') {
        setInputValue('[paste mode — paste and press Enter]');
        readLineCooked().then((pasted) => {
          if (pasted) {
            setInputValue('');
            useUIStore.getState().setDialog(null);
            adapterRef.current?.sendDialogChoice(pasted);
          } else {
            setInputValue('');
          }
        });
        return;
      }
      if (key.return) {
        const text = inputRef.current.trim();
        if (text) {
          setInputValue('');
          adapterRef.current?.sendDialogChoice(text);
          useUIStore.getState().setDialog(null);
        }
        return;
      }
      if (key.backspace || key.delete) {
        setInputValue((v) => v.slice(0, -1));
        return;
      }
      if (_input && !key.meta) {
        setInputValue((v) => v + _input);
        return;
      }
      return;
    }

    // ── Keyboard shortcuts (only when no dialog) ──
    if (key.ctrl) {
      if (_input === 's') { useUIStore.getState().toggleSidebar(); return; }
      if (_input === 't') { useUIStore.getState().toggleTimestamps(); return; }
      if (_input === 'e') { useUIStore.getState().toggleExpandAll(); return; }
      if (_input === 'c') { process.exit(0); return; }
      if (_input === 'p') {
        readLineCooked().then((pasted) => {
          if (pasted) setInputValue(pasted);
        });
        return;
      }
      return;
    }
    if (key.pageUp) { useUIStore.getState().scrollUp(); return; }
    if (key.pageDown) { useUIStore.getState().scrollDown(); return; }

    // ── Normal input mode ──

    // Arrow key navigation for suggestion dropdown
    if (suggestions.length > 0) {
      if (key.upArrow) {
        setSelectedSuggestion((i) => (i > 0 ? i - 1 : suggestions.length - 1));
        return;
      }
      if (key.downArrow) {
        setSelectedSuggestion((i) => (i < suggestions.length - 1 ? i + 1 : 0));
        return;
      }
      if (key.return || key.tab) {
        const cmd = suggestions[clampedSelected].cmd;
        setInputValue('/' + cmd + ' ');
        setSelectedSuggestion(0);
        return;
      }
    }

    // Up/down arrow history navigation (when no suggestions showing)
    if ((key.upArrow || key.downArrow) && history.length > 0) {
      const last = history.length - 1;
      let newIdx: number;
      if (historyIndex === -1) {
        // First arrow press: save current input and start from end
        savedInput.current = inputRef.current;
        newIdx = key.downArrow ? last : last;
      } else {
        newIdx = key.upArrow ? historyIndex - 1 : historyIndex + 1;
        if (newIdx < 0) { savedInput.current = ''; setInputValue(''); setHistoryIndex(-1); return; }
        if (newIdx >= last) { newIdx = last; }
      }
      setHistoryIndex(newIdx);
      setInputValue(history[newIdx]);
      return;
    }

    if (key.return) {
      const text = inputRef.current.trim();
      if (!text) return;
      // Add to history (avoid duplicate consecutive entries)
      if (history.length === 0 || history[history.length - 1] !== text) {
        setHistory((h) => [...h.slice(-99), text]);
      }
      setInputValue('');
      setSelectedSuggestion(0);
      setHistoryIndex(-1);
      savedInput.current = '';

      if (text.startsWith('/')) {
        const cmd = text.slice(1).split(' ')[0].toLowerCase();
        if (cmd === 'exit' || cmd === 'quit') {
          process.exit(0);
          return;
        }
      }

      adapterRef.current?.sendInput(text);
      return;
    }
    if (key.backspace || key.delete) {
      setInputValue((v) => v.slice(0, -1));
      return;
    }
    if (key.tab) {
      if (!inputRef.current.trim()) {
        // Toggle agent mode
        adapterRef.current?.sendInput('/mode');
        return;
      }
      setInputValue((v) => v + '    ');
      return;
    }
    if (key.escape) return;

    if (_input && !key.meta) {
      setInputValue((v) => v + _input);
    }
  });

  return (
    <Box flexDirection="column" height="100%">
      <Header />
      {lastError ? (
        <Text color={colors.error}>error: {lastError}</Text>
      ) : null}
      <MessageViewport />
      <StatusBar />
      <InputBar value={inputRef.current} suggestions={suggestions.slice(0, 10)} selectedIndex={clampedSelected} />
      {dialog ? <Dialog /> : null}
    </Box>
  );
}
