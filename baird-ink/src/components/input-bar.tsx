import { Box, Text } from 'ink';
import { colors, BAR } from '../theme.js';
import { useUIStore } from '../store/index.js';

interface Props {
  value: string;
}

const COMMANDS: { cmd: string; desc: string }[] = [
  { cmd: 'exit', desc: 'Exit the REPL' },
  { cmd: 'quit', desc: 'Exit the REPL' },
  { cmd: 'help', desc: 'Show help' },
  { cmd: 'context', desc: 'Show current context' },
  { cmd: 'reset', desc: 'Start a new session' },
  { cmd: 'cost', desc: 'Show token usage and cost' },
  { cmd: 'model', desc: 'Switch model' },
  { cmd: 'project', desc: 'List / switch projects' },
  { cmd: 'sessions', desc: 'List recent sessions' },
  { cmd: 'connect', desc: 'Connect an API provider (OpenRouter/OpenCode Zen)' },
  { cmd: 'no-diff', desc: 'Disable diff approval prompts' },
];

/** Filter commands by a partial input string. */
function matching(cmds: typeof COMMANDS, partial: string): typeof COMMANDS {
  if (!partial) return cmds;
  const p = partial.toLowerCase();
  return cmds.filter((c) => c.cmd.startsWith(p));
}

/**
 * Input bar display — renders the prompt line and slash-command suggestions.
 * Input handling lives in App.tsx via a single useInput hook.
 */
export function InputBar({ value }: Props) {
  const dialog = useUIStore((s) => s.dialog);
  const display = value || 'Type a message…';

  // Slash command suggestions
  const showSuggestions = value.startsWith('/') && value.length > 1;
  const partial = value.slice(1); // strip leading /
  const suggestions = showSuggestions ? matching(COMMANDS, partial) : [];
  const showDropdown = suggestions.length > 0 && suggestions.length < COMMANDS.length;

  return (
    <Box flexDirection="column">
      <Box>
        <Text color={colors.textMuted}>{BAR}  </Text>
        <Text color={value ? colors.text : colors.textMuted}>
          {dialog ? '[dialog active — press Esc]' : display}
        </Text>
      </Box>
      {showDropdown ? (
        <Box flexDirection="column" marginLeft={4}>
          {suggestions.slice(0, 10).map((s) => (
            <Text key={s.cmd} color={colors.secondary}>
              /{s.cmd}  <Text color={colors.textMuted}>{s.desc}</Text>
            </Text>
          ))}
        </Box>
      ) : null}
    </Box>
  );
}
