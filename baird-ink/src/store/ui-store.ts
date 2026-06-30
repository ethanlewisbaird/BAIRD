import { create } from 'zustand';
import type { Dialog } from '../types/index.js';

export interface UIStore {
  terminalWidth: number;
  terminalHeight: number;
  scrollOffset: number;
  sidebarVisible: boolean;
  showTimestamps: boolean;
  allToolsExpanded: boolean;
  dialog: Dialog | null;
  spinnerFrame: number;

  scrollUp(): void;
  scrollDown(): void;
  scrollToBottom(): void;
  toggleSidebar(): void;
  toggleTimestamps(): void;
  toggleExpandAll(): void;
  setDialog(d: Dialog | null): void;
  tickSpinner(): void;
}

export const useUIStore = create<UIStore>((set) => ({
  terminalWidth: 80,
  terminalHeight: 24,
  scrollOffset: 0,
  sidebarVisible: false,
  showTimestamps: false,
  allToolsExpanded: false,
  dialog: null,
  spinnerFrame: 0,

  scrollUp: () => set((s) => ({ scrollOffset: Math.min(s.scrollOffset + 4, 999) })),
  scrollDown: () => set((s) => ({ scrollOffset: Math.max(0, s.scrollOffset - 4) })),
  scrollToBottom: () => set({ scrollOffset: 0 }),
  toggleSidebar: () => set((s) => ({ sidebarVisible: !s.sidebarVisible })),
  toggleTimestamps: () => set((s) => ({ showTimestamps: !s.showTimestamps })),
  toggleExpandAll: () => set((s) => ({ allToolsExpanded: !s.allToolsExpanded })),
  setDialog: (dialog) => set({ dialog }),
  tickSpinner: () => set((s) => ({ spinnerFrame: (s.spinnerFrame + 1) % 10 })),
}));
