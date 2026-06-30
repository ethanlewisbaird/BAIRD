/** Events emitted by the backend adapter and consumed by the store reducer */

import type { ToolState } from './index.js';

// ── Stream events (from LLM) ──

export interface TextDeltaEvent {
  kind: 'text_delta';
  delta: string;
}

export interface ToolCallBeginEvent {
  kind: 'tool_call_begin';
  id: string;
  name: string;
  arguments: string;
}

export interface ToolCallArgsEvent {
  kind: 'tool_call_args';
  id: string;
  argsDelta: string;
}

export interface StreamEndEvent {
  kind: 'stream_end';
  usage?: { inputTokens: number; outputTokens: number; costUsd: number };
}

// ── Tool events ──

export interface ToolStartedEvent {
  kind: 'tool_started';
  invocationId: string;
}

export interface ToolOutputEvent {
  kind: 'tool_output';
  invocationId: string;
  chunk: string;
}

export interface ToolCompletedEvent {
  kind: 'tool_completed';
  invocationId: string;
}

export interface ToolFailedEvent {
  kind: 'tool_failed';
  invocationId: string;
  error: string;
}

export interface ToolCancelledEvent {
  kind: 'tool_cancelled';
  invocationId: string;
}

// ── Session events ──

export interface TurnStartEvent {
  kind: 'turn_start';
}

export interface UserMessageEvent {
  kind: 'user_message';
  text: string;
}

export interface StatusEvent {
  kind: 'status';
  text: string;
}

export interface ErrorEvent {
  kind: 'error';
  text: string;
}

export interface DialogEvent {
  kind: 'dialog';
  id: string;
  title: string;
  body: string;
  choices: string[];
}

export interface DialogDismissEvent {
  kind: 'dialog_dismiss';
}

export interface StatsUpdateEvent {
  kind: 'stats_update';
  turns: number;
  costUsd: number;
  inputTokens: number;
  outputTokens: number;
}

// ── Model info ──

export interface ModelInfoEvent {
  kind: 'model_info';
  model: string;
  agentMode: string;
}

// ── Union ──

export type StreamEvent =
  | TextDeltaEvent
  | ToolCallBeginEvent
  | ToolCallArgsEvent
  | StreamEndEvent;

export type ToolEvent =
  | ToolStartedEvent
  | ToolOutputEvent
  | ToolCompletedEvent
  | ToolFailedEvent
  | ToolCancelledEvent;

export type BackendEvent =
  | StreamEvent
  | ToolEvent
  | TurnStartEvent
  | UserMessageEvent
  | StatusEvent
  | ErrorEvent
  | DialogEvent
  | DialogDismissEvent
  | StatsUpdateEvent
  | ModelInfoEvent;
