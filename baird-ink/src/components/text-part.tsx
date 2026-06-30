import { Text } from 'ink';
import type { TextPart as TextPartType } from '../types/index.js';
import { colors, TEXT_PART_PADDING_LEFT } from '../theme.js';

interface Props {
  part: TextPartType;
}

export function TextPart({ part }: Props) {
  const indent = ' '.repeat(TEXT_PART_PADDING_LEFT);
  const lines = part.text.split('\n');
  return (
    <Text>
      {lines.map((line, i) => (
        <Text key={i} color={colors.text}>
          {i > 0 ? '\n' : ''}{indent}{line}
        </Text>
      ))}
    </Text>
  );
}
