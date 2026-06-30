import { Box, Text } from 'ink';
import { colors, BAR } from '../theme.js';
import { useSessionStore } from '../store/index.js';

export function StatusBar() {
  const stats = useSessionStore((s) => s.stats);

  return (
    <Box>
      <Text color={colors.textMuted}>{BAR}  </Text>
      <Text color={colors.textMuted}>
        turns={stats.turns}  cost=${stats.totalCostUsd.toFixed(4)}  tokens={stats.totalInputTokens}→{stats.totalOutputTokens}
      </Text>
    </Box>
  );
}
