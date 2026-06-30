import { create } from 'zustand';
import type { Message, ReplStats } from '../types/index.js';
import type { BackendEvent } from '../types/events.js';
import { reduceEvent } from './reducers.js';

export interface SessionStore {
  messages: Message[];
  agentMode: string;
  model: string;
  stats: ReplStats;
  projectDisplay: string;
  hostDisplay: string;
  branchDisplay: string | null;
  sessionId: string;
  lastStatus: string;
  lastError: string;

  dispatchEvent(event: BackendEvent): void;
  setSessionInfo(info: {
    agentMode: string;
    model: string;
    projectDisplay: string;
    hostDisplay: string;
    branchDisplay: string | null;
    sessionId: string;
  }): void;
}

export const useSessionStore = create<SessionStore>((set, get) => ({
  messages: [],
  agentMode: 'build',
  model: '',
  stats: { turns: 0, totalCostUsd: 0, totalInputTokens: 0, totalOutputTokens: 0 },
  projectDisplay: '',
  hostDisplay: '',
  branchDisplay: null,
  sessionId: '',
  lastStatus: '',
  lastError: '',

  dispatchEvent: (event: BackendEvent) => {
    const state = get();
    const patch = reduceEvent(state, event);
    set(patch);
  },

  setSessionInfo: (info) => {
    set(info);
  },
}));
