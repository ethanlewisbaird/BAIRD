/**
 * Pure event → state transitions.
 * Each function takes (state, event) and returns new state.
 * No side effects, no mutations.
 */

import type { Message, Part, TextPart, ToolInvocationPart, ToolOutputPart, ToolState } from '../types/index.js';
import type { BackendEvent } from '../types/events.js';
import type { SessionStore } from './session-store.js';

/** Map a tool icon character from the backend event. */
function toolIcon(name: string): string {
  const shell = ['run_on', 'read_remote', 'write_remote', 'apply_diff_remote'];
  const readL = ['read_file', 'list_projects', 'list_project_locations', 'read_remote'];
  const writeL = ['write_file', 'apply_diff', 'edit_file'];
  const search = ['glob', 'grep', 'find'];
  const web = ['websearch', 'research'];
  const fetchL = ['webfetch', 'fetch'];
  const mgmt = ['register_project', 'add_project_location', 'todowrite', 'set_watch_root'];

  if (shell.includes(name) || name.startsWith('run_')) return '$';
  if (readL.includes(name)) return '\u2192';
  if (writeL.includes(name)) return '\u2190';
  if (search.includes(name)) return '\u2699';
  if (web.includes(name)) return '\u25C7';
  if (fetchL.includes(name)) return '%';
  if (mgmt.includes(name)) return '\u2699';
  return '\u2699';
}

/** Generate a short unique id. */
let _counter = 0;
function uid(): string {
  return `m${Date.now().toString(36)}_${(++_counter).toString(36)}`;
}

/** Get the active (last) assistant message if it's streaming. */
function activeMsg(state: SessionStore): Message | undefined {
  const m = state.messages[state.messages.length - 1];
  return m?.role === 'assistant' && m.streaming ? m : undefined;
}

/** Merge text delta into the last TextPart of the active message. */
function mergeTextDelta(parts: Part[], delta: string): Part[] {
  if (parts.length > 0) {
    const last = parts[parts.length - 1];
    if (last.kind === 'text') {
      const updated: Part[] = [...parts];
      updated[updated.length - 1] = { ...last, text: last.text + delta };
      return updated;
    }
  }
  return [...parts, { kind: 'text', text: delta }];
}

/** Find index of a ToolInvocationPart by id. */
function findInvocationIndex(parts: Part[], id: string): number {
  return parts.findIndex(
    (p): p is ToolInvocationPart => p.kind === 'tool_invocation' && p.id === id
  );
}

/** Update invocation state in-place. */
function updateInvocationState(parts: Part[], id: string, state: ToolState): Part[] {
  const idx = findInvocationIndex(parts, id);
  if (idx === -1) return parts;
  const updated = [...parts];
  const p = updated[idx] as ToolInvocationPart;
  updated[idx] = { ...p, state };
  return updated;
}

// ── Reducer ──

export function reduceEvent(state: SessionStore, event: BackendEvent): Partial<SessionStore> {
  switch (event.kind) {
    // ── User message ──
    case 'user_message': {
      const msg: Message = {
        id: uid(),
        role: 'user',
        content: event.text,
        parts: [],
        streaming: false,
        timestamp: Date.now() / 1000,
      };
      return { messages: [...state.messages, msg] };
    }

    // ── Turn start ──
    case 'turn_start': {
      const msg: Message = {
        id: uid(),
        role: 'assistant',
        content: '',
        parts: [],
        streaming: true,
        agentMode: state.agentMode,
        model: state.model,
        timestamp: Date.now() / 1000,
      };
      return { messages: [...state.messages, msg] };
    }

    // ── Text delta ──
    case 'text_delta': {
      const msg = activeMsg(state);
      if (!msg) return {};
      const parts = mergeTextDelta(msg.parts, event.delta);
      return {
        messages: state.messages.map((m) =>
          m.id === msg.id ? { ...m, parts, content: m.content + event.delta } : m
        ),
      };
    }

    // ── Tool call begin ──
    case 'tool_call_begin': {
      const msg = activeMsg(state);
      if (!msg) return {};
      const parts: Part[] = [
        ...msg.parts,
        {
          kind: 'tool_invocation',
          id: event.id,
          tool: event.name,
          args: event.arguments,
          icon: toolIcon(event.name),
          state: 'queued',
        } satisfies ToolInvocationPart,
      ];
      return { messages: state.messages.map((m) => (m.id === msg.id ? { ...m, parts } : m)) };
    }

    // ── Tool call args (append to existing invocation) ──
    case 'tool_call_args': {
      const msg = activeMsg(state);
      if (!msg) return {};
      const idx = findInvocationIndex(msg.parts, event.id);
      if (idx === -1) return {};
      const parts = [...msg.parts];
      const p = parts[idx] as ToolInvocationPart;
      parts[idx] = { ...p, args: p.args + event.argsDelta };
      return { messages: state.messages.map((m) => (m.id === msg.id ? { ...m, parts } : m)) };
    }

    // ── Tool started ──
    case 'tool_started': {
      const msg = activeMsg(state);
      if (!msg) return {};
      return {
        messages: state.messages.map((m) =>
          m.id === msg.id
            ? { ...m, parts: updateInvocationState(m.parts, event.invocationId, 'running') }
            : m
        ),
      };
    }

    // ── Tool output ──
    case 'tool_output': {
      const msg = activeMsg(state);
      if (!msg) return {};
      const parts: Part[] = [
        ...msg.parts,
        {
          kind: 'tool_output',
          toolInvocationId: event.invocationId,
          output: event.chunk,
          stream: true,
        } satisfies ToolOutputPart,
      ];
      return { messages: state.messages.map((m) => (m.id === msg.id ? { ...m, parts } : m)) };
    }

    // ── Tool completed ──
    case 'tool_completed': {
      const msg = activeMsg(state);
      if (!msg) return {};
      return {
        messages: state.messages.map((m) =>
          m.id === msg.id
            ? { ...m, parts: updateInvocationState(m.parts, event.invocationId, 'success') }
            : m
        ),
      };
    }

    // ── Tool failed ──
    case 'tool_failed': {
      const msg = activeMsg(state);
      if (!msg) return {};
      const parts = updateInvocationState(msg.parts, event.invocationId, 'error');
      const partsWithOutput: Part[] = [
        ...parts,
        { kind: 'tool_output', toolInvocationId: event.invocationId, output: event.error, stream: false } satisfies ToolOutputPart,
      ];
      return {
        messages: state.messages.map((m) =>
          m.id === msg.id ? { ...m, parts: partsWithOutput } : m
        ),
      };
    }

    // ── Tool cancelled ──
    case 'tool_cancelled': {
      const msg = activeMsg(state);
      if (!msg) return {};
      return {
        messages: state.messages.map((m) =>
          m.id === msg.id
            ? { ...m, parts: updateInvocationState(m.parts, event.invocationId, 'cancelled') }
            : m
        ),
      };
    }

    // ── Stream end ──
    case 'stream_end': {
      const msg = activeMsg(state);
      if (!msg) return {};
      const now = Date.now() / 1000;
      const duration = msg.timestamp ? now - msg.timestamp : 0;
      const stats = { ...state.stats };
      if (event.usage) {
        stats.totalInputTokens += event.usage.inputTokens;
        stats.totalOutputTokens += event.usage.outputTokens;
        stats.totalCostUsd += event.usage.costUsd;
      }
      return {
        messages: state.messages.map((m) =>
          m.id === msg.id ? { ...m, streaming: false, duration } : m
        ),
        stats,
      };
    }

    // ── Status ──
    case 'status': {
      return { lastStatus: event.text };
    }

    // ── Error ──
    case 'error': {
      return { lastError: event.text };
    }

    // ── Stats update ──
    case 'stats_update': {
      return {
        stats: {
          turns: event.turns,
          totalCostUsd: event.costUsd,
          totalInputTokens: event.inputTokens,
          totalOutputTokens: event.outputTokens,
        },
      };
    }

    // ── Model info ──
    case 'model_info': {
      return { model: event.model, agentMode: event.agentMode };
    }

    default:
      return {};
  }
}
