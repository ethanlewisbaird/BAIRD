import { Box, Text } from 'ink';
import type { Message as Msg } from '../types/index.js';
import { colors, BAR } from '../theme.js';
import { useUIStore } from '../store/index.js';
import { TextPart as TextPartComp } from './text-part.js';
import { ToolInvocation } from './tool-invocation.js';
import { ToolOutput } from './tool-output.js';
import { ReasoningPart } from './reasoning-part.js';

interface Props {
  msg: Msg;
}

/** Format a unix timestamp to HH:MM:SS. */
function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-US', { hour12: false });
}

export function Message({ msg }: Props) {
  const showTimestamps = useUIStore((s) => s.showTimestamps);

  if (msg.role === 'user') {
    return (
      <Box flexDirection="column">
        {showTimestamps && msg.timestamp ? (
          <Text color={colors.textMuted}>   {fmtTime(msg.timestamp)}</Text>
        ) : null}
        <Box>
          <Text color={colors.primary}>{BAR}  </Text>
          <Text color={colors.text}>{msg.content}</Text>
        </Box>
      </Box>
    );
  }

  // Assistant message
  return (
    <Box flexDirection="column">
      {showTimestamps && msg.timestamp ? (
        <Text color={colors.textMuted}>   {fmtTime(msg.timestamp)}</Text>
      ) : null}
      {msg.parts.map((part, i) => {
        switch (part.kind) {
          case 'text':
            return <TextPartComp key={i} part={part} />;
          case 'reasoning':
            return <ReasoningPart key={i} part={part} />;
          case 'tool_invocation':
            return <ToolInvocation key={`inv-${part.id}`} part={part} />;
          case 'tool_output':
            return <ToolOutput key={`out-${part.toolInvocationId}-${i}`} part={part} />;
          default:
            return null;
        }
      })}
      {/* Footer: agent badge + model + duration */}
      {(msg.agentMode || msg.model || (msg.duration ?? 0) > 0) && (
        <Box>
          <Text color={colors.textMuted}>{BAR}  </Text>
          {msg.agentMode ? (
            <Text backgroundColor={colors.primary as any} color={colors.background} bold>
              {' '}{msg.agentMode.toUpperCase()}{' '}
            </Text>
          ) : null}
          {msg.model ? (
            <Text color={colors.textMuted}>  {msg.model}  </Text>
          ) : null}
          {msg.duration ? (
            <Text color={colors.textMuted}>{msg.duration.toFixed(1)}s</Text>
          ) : null}
          {msg.streaming ? (
            <Text color={colors.textMuted}>  streaming…</Text>
          ) : null}
        </Box>
      )}
    </Box>
  );
}
