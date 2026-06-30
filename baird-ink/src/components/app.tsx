import { useState, useRef, useEffect } from 'react';
import { Box, Text, useInput } from 'ink';
import { Header } from './header.js';
import { MessageViewport } from './message-viewport.js';
import { Message } from './message.js';
import { StatusBar } from './status-bar.js';
import { InputBar } from './input-bar.js';
import { Dialog } from './dialog.js';
import { useSessionStore, useUIStore } from '../store/index.js';
import { startBackend, type BackendAdapter } from '../adapter/backend.js';
import { SPINNER_INTERVAL, colors } from '../theme.js';
import type { BackendEvent } from '../types/events.js';

export function App() {
  const messages = useSessionStore((s) => s.messages);
  const lastError = useSessionStore((s) => s.lastError);
  const dialog = useUIStore((s) => s.dialog);

  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef(inputValue);
  inputRef.current = inputValue;

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
      // Text-input dialog (Escape ignored — bracketed paste conflict)
      if (key.ctrl && _input === 'c') { process.exit(0); return; }
      if (key.ctrl && _input === 'p') {
        useUIStore.getState().setDialog(null);
        readLineCooked().then((pasted) => {
          if (pasted) {
            adapterRef.current?.sendDialogChoice(pasted);
          }
        });
        return;
      }
      if (key.return) {
        const text = inputRef.current.trim();
        if (text) {
          adapterRef.current?.sendDialogChoice(text);
          useUIStore.getState().setDialog(null);
          setInputValue('');
        }
        return;
      }
      if (key.backspace || key.delete) {
        setInputValue((v) => v.slice(0, -1));
        return;
      }
      // Accept any character (Ctrl+V, paste chunks, regular typing)
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
    if (key.return) {
      const text = inputRef.current.trim();
      if (!text) return;
      setInputValue('');

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
        // TODO: toggle agent mode
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
      <MessageViewport>
        {messages.map((msg) => (
          <Message key={msg.id} msg={msg} />
        ))}
        {lastError ? (
          <Text color={colors.error}>error: {lastError}</Text>
        ) : null}
      </MessageViewport>
      <StatusBar />
      <InputBar value={inputRef.current} />
      {dialog ? <Dialog /> : null}
    </Box>
  );
}
