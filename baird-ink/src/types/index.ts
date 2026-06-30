/** Core message and part types — event-sourced with state machine for invocations */

// ── Agent mode ──

export enum AgentMode {
  BUILD = 'build',
  PLAN = 'plan',
  AUTO = 'auto',
}

export const AgentModeBadge: Record<AgentMode, string> = {
  [AgentMode.BUILD]: 'BUILD',
  [AgentMode.PLAN]: 'PLAN',
  [AgentMode.AUTO]: 'AUTO',
};

// ── Conversation stats ──

export interface ReplStats {
  turns: number;
  totalCostUsd: number;
  totalInputTokens: number;
  totalOutputTokens: number;
}

// ── Dialog ──

export interface Dialog {
  kind: 'confirm' | 'prompt' | 'info';
  title: string;
  body: string;
  choices: string[];
  result: string | null;
}

// ── Part types ──

export interface TextPart {
  kind: 'text';
  text: string;
}

export interface ReasoningPart {
  kind: 'reasoning';
  text: string;
  title: string;
  done: boolean;
}

export type ToolState = 'queued' | 'running' | 'success' | 'error' | 'cancelled';

export interface ToolInvocationPart {
  kind: 'tool_invocation';
  id: string;
  tool: string;
  args: string;
  icon: string;
  state: ToolState;
}

export interface ToolOutputPart {
  kind: 'tool_output';
  toolInvocationId: string;
  output: string;
  stream: boolean;
}

export type Part = TextPart | ReasoningPart | ToolInvocationPart | ToolOutputPart;

// ── Message ──

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  parts: Part[];
  streaming: boolean;
  agentMode?: string;
  model?: string;
  duration?: number;
  timestamp?: number;
}
