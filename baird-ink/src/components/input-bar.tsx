import { Box, Text } from 'ink';
import { colors, BAR } from '../theme.js';
import { useUIStore } from '../store/index.js';

interface Props {
  value: string;
}

/**
 * Input bar display — renders the prompt line.
 * Input handling lives in App.tsx via a single useInput hook.
 */
export function InputBar({ value }: Props) {
  const dialog = useUIStore((s) => s.dialog);
  const display = value || 'Type a message…';

  return (
    <Box>
      <Text color={colors.textMuted}>{BAR}  </Text>
      <Text color={value ? colors.text : colors.textMuted}>
        {dialog ? '[dialog active — press Esc]' : display}
      </Text>
    </Box>
  );
}
