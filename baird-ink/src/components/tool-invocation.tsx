import { Text } from 'ink';
import type { ToolInvocationPart, ToolState } from '../types/index.js';
import { colors, SPINNER_FRAMES, BAR } from '../theme.js';
import { useUIStore } from '../store/index.js';

interface Props {
  part: ToolInvocationPart;
}

const STATE_STYLES: Record<ToolState, { color: string; prefix: string }> = {
  queued: { color: colors.textMuted, prefix: '  ' },
  running: { color: colors.secondary, prefix: '' },
  success: { color: colors.success, prefix: '  ' },
  error: { color: colors.error, prefix: '  ' },
  cancelled: { color: colors.textMuted, prefix: '  ' },
};

export function ToolInvocation({ part }: Props) {
  const spinnerFrame = useUIStore((s) => s.spinnerFrame);
  const style = STATE_STYLES[part.state];

  const spinner = part.state === 'running'
    ? SPINNER_FRAMES[spinnerFrame % SPINNER_FRAMES.length]
    : '';

  const argsPreview = part.args.length > 100
    ? part.args.slice(0, 100) + '…'
    : part.args;

  const icon = part.state === 'running' ? spinner : `${style.prefix}${part.icon}`;

  return (
    <Text>
      <Text color={colors.textMuted}>{BAR}  </Text>
      <Text color={style.color}>{icon} {part.tool}({argsPreview})</Text>
      <Text color={colors.textMuted}> [{part.state}]</Text>
    </Text>
  );
}
