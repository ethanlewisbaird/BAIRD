/** OpenCode colour palette — exact hex values from Python theme.py */

export const colors = {
  background: '#0a0a0a',
  backgroundPanel: '#141414',
  backgroundElement: '#1e1e1e',
  backgroundMenu: '#1e1e1e',

  text: '#eeeeee',
  textMuted: '#808080',

  primary: '#fab283',
  secondary: '#5c9cf5',
  accent: '#9d7cd8',

  error: '#e06c75',
  warning: '#f5a742',
  success: '#7fd88f',
  info: '#56b6c2',

  border: '#484848',
  borderActive: '#606060',
  borderSubtle: '#3c3c3c',

  diffAdded: '#7fd88f',
  diffRemoved: '#e06c75',
  diffContext: '#808080',
  diffHunkHeader: '#5c9cf5',

  diffAddedBg: '#1a2e1a',
  diffRemovedBg: '#2e1a1a',

  syntaxComment: '#6a9955',
  syntaxString: '#ce9178',
  syntaxNumber: '#b5cea8',
  syntaxKeyword: '#569cd6',
  syntaxFunction: '#dcdcaa',
  syntaxType: '#4ec9b0',
} as const;

/** Spinner frames (same as OpenCode) */
export const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
export const SPINNER_INTERVAL = 80; // ms

/** Unicode glyphs */
export const BAR = '\u2502';
export const BAR_THICK = '\u2503';
export const CHECK = '\u2713';
export const CROSS = '\u2717';
export const BULLET = '\u25CF';
export const AGENT_ICON = '\u25A3';

/** Spacing constants (exact OpenCode values) */
export const CONTENT_PADDING_LEFT = 2;
export const CONTENT_PADDING_RIGHT = 2;
export const CONTENT_PADDING_BOTTOM = 1;
export const CONTENT_GAP = 1;

export const MSG_PADDING_TOP = 1;
export const MSG_PADDING_BOTTOM = 1;
export const MSG_PADDING_LEFT = 2;
export const MSG_MARGIN_TOP = 1;

export const TEXT_PART_PADDING_LEFT = 3;

export const SIDEBAR_WIDTH = 42;
