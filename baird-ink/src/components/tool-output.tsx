import { Box, Text } from 'ink';
import type { ToolOutputPart } from '../types/index.js';
import { colors, BAR, SPINNER_FRAMES } from '../theme.js';
import { useUIStore } from '../store/index.js';

interface Props {
  part: ToolOutputPart;
}

export function ToolOutput({ part }: Props) {
  const allExpanded = useUIStore((s) => s.allToolsExpanded);

  const lines = part.output.split('\n');
  const maxLines = allExpanded ? lines.length : 8;
  const truncated = lines.length > maxLines;

  return (
    <Box flexDirection="column">
      {lines.slice(0, maxLines).map((line, i) => (
        <Box key={i}>
          <Text color={colors.textMuted}>{BAR}  </Text>
          <Text color={colors.text}>{line}</Text>
        </Box>
      ))}
      {truncated ? (
        <Box>
          <Text color={colors.textMuted}>{BAR}  </Text>
          <Text color={colors.textMuted}>
            ... ({lines.length - maxLines} more lines, press Ctrl+E to expand)
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}
