import { Box, Text } from 'ink';
import { useEffect } from 'react';
import { useUIStore, useSessionStore } from '../store/index.js';
import { Message } from './message.js';
import { colors } from '../theme.js';

/**
 * Message viewport — renders the visible window of messages.
 * Removes the fixed-height constraint so text doesn't overlap.
 * The terminal's built-in scrollback handles viewing older content.
 */
export function MessageViewport() {
  const messages = useSessionStore((s) => s.messages);
  const scrollOffset = useUIStore((s) => s.scrollOffset);

  // Auto-scroll to bottom while streaming
  const streaming = messages.length > 0 && messages[messages.length - 1].streaming;
  useEffect(() => {
    if (streaming) useUIStore.getState().scrollToBottom();
  }, [streaming]);

  // How many messages to show from the end.
  // scrollOffset of 0 = show 20; each scroll increment adds 10 more.
  const baseCount = 20 + scrollOffset * 10;
  const visibleCount = Math.max(10, Math.min(messages.length, baseCount));

  const visible = messages.slice(Math.max(0, messages.length - visibleCount));

  return (
    <Box flexDirection="column">
      {messages.length > visibleCount ? (
        <Text color={colors.textMuted}>
          {messages.length - visibleCount} earlier messages — PgUp to see more
        </Text>
      ) : null}
      {visible.map((msg) => (
        <Message key={msg.id} msg={msg} />
      ))}
    </Box>
  );
}
