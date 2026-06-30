import { Text } from 'ink';
import type { ReasoningPart as ReasoningPartType } from '../types/index.js';
import { colors, SPINNER_FRAMES } from '../theme.js';

interface Props {
  part: ReasoningPartType;
}

export function ReasoningPart({ part }: Props) {
  const icon = part.done ? '\u25B8' : SPINNER_FRAMES[0];
  const lines = part.text.split('\n');

  return (
    <Text>
      <Text color={colors.textMuted}>{icon} {part.title}</Text>
      {lines.map((line, i) => (
        <Text key={i} color={colors.textMuted} dimColor>
          {'\n'}   {line}
        </Text>
      ))}
    </Text>
  );
}
