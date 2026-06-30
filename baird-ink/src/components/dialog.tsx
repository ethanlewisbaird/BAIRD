import { Box, Text } from 'ink';
import { colors } from '../theme.js';
import { useUIStore } from '../store/index.js';

export function Dialog() {
  const dialog = useUIStore((s) => s.dialog);
  if (!dialog) return null;

  return (
    <Box
      borderStyle="round"
      borderColor={colors.warning as any}
      paddingX={2}
      paddingY={1}
      marginTop={1}
    >
      <Box flexDirection="column">
        <Text bold color={colors.text}>{dialog.title}</Text>
        <Text color={colors.text}>{dialog.body}</Text>
        {dialog.choices.length > 0 ? (
          <Box flexDirection="column">
            {dialog.choices.map((c, i) => (
              <Text key={i} color={colors.textMuted}>  {i + 1}. {c}</Text>
            ))}
          </Box>
        ) : null}
      </Box>
    </Box>
  );
}
