import { Box, Text } from 'ink';
import { colors, BAR_THICK } from '../theme.js';
import { useSessionStore } from '../store/index.js';

const AGENT_COLORS: Record<string, string> = {
  build: colors.primary,
  plan: colors.secondary,
  auto: colors.accent,
};

export function Header() {
  const agentMode = useSessionStore((s) => s.agentMode);
  const projectDisplay = useSessionStore((s) => s.projectDisplay);
  const model = useSessionStore((s) => s.model);

  const badge = agentMode.toUpperCase();
  const badgeColor = AGENT_COLORS[agentMode] ?? colors.textMuted;

  return (
    <Box>
      <Text color={colors.textMuted}>{BAR_THICK} </Text>
      <Box>
        <Text backgroundColor={badgeColor as any} color={colors.background} bold>
          {' '}{badge}{' '}
        </Text>
      </Box>
      <Text color={colors.textMuted}>  </Text>
      <Text color={colors.text}>{projectDisplay || '?'}</Text>
      <Text color={colors.textMuted}>  │  </Text>
      <Text color={colors.textMuted}>{model}</Text>
    </Box>
  );
}
