import { Box } from 'ink';
import type { ReactNode } from 'react';
import { useUIStore } from '../store/index.js';
import type { Message, Part } from '../types/index.js';

interface Props {
  children: ReactNode;
}

/** Estimate rendered line count for a message (used for viewport clipping). */
function estimateLines(msg: Message): number {
  let lines = 0;
  for (const p of msg.parts) {
    switch (p.kind) {
      case 'text':
        lines += (p.text.match(/\n/g) || []).length + 1;
        break;
      case 'reasoning':
        lines += (p.text.match(/\n/g) || []).length + 2;
        break;
      case 'tool_invocation':
        lines += 1;
        break;
      case 'tool_output':
        lines += Math.min((p.output.match(/\n/g) || []).length + 1, 9);
        break;
    }
  }
  // Footer (agent badge + model)
  if (msg.agentMode || msg.model) lines += 1;
  return lines + 1; // +1 for the message itself
}

/**
 * Message viewport — renders only the visible slice of children
 * based on scrollOffset.
 */
export function MessageViewport({ children }: Props) {
  const terminalHeight = useUIStore((s) => s.terminalHeight);
  const scrollOffset = useUIStore((s) => s.scrollOffset);

  // Available lines: terminal - header(1) - status(1) - input(1) - padding(1)
  const availLines = Math.max(5, terminalHeight - 4);

  // Children are <Message> components. We need to clip by estimated lines.
  // Wrap children in a container that limits its visual height.
  return (
    <Box
      flexGrow={1}
      flexDirection="column"
      height={availLines}
      overflowY="hidden"
    >
      {children}
    </Box>
  );
}
