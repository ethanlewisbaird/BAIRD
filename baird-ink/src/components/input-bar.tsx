import { Box, Text } from 'ink';
import { colors, BAR } from '../theme.js';
import { useUIStore } from '../store/index.js';

interface Props {
  value: string;
  suggestions: { cmd: string; desc: string }[];
  selectedIndex: number;
}

export const COMMANDS: { cmd: string; desc: string }[] = [
  // Local commands
  { cmd: 'exit', desc: 'Exit the REPL' },
  { cmd: 'quit', desc: 'Exit the REPL' },
  { cmd: 'help', desc: 'Show help' },
  { cmd: 'context', desc: 'Show current context' },
  { cmd: 'reset', desc: 'Start a new session' },
  { cmd: 'cost', desc: 'Show token usage and cost' },
  { cmd: 'model', desc: 'Switch model' },
  { cmd: 'sessions', desc: 'List recent sessions' },
  { cmd: 'no-diff', desc: 'Disable diff approval prompts' },
  { cmd: 'connect', desc: 'Connect an API provider' },
  // Hub-first commands (from baird/slash.py)
  { cmd: 'project', desc: 'List / switch projects' },
  { cmd: 'project new', desc: 'Create a new project on the hub' },
  { cmd: 'project rename', desc: 'Rename a project' },
  { cmd: 'project delete', desc: 'Delete a project (destructive)' },
  { cmd: 'project add-location', desc: 'Add a location to a project' },
  { cmd: 'project locations', desc: 'List project locations on satellites' },
  { cmd: 'project enrich', desc: 'Probe satellite paths for project metadata' },
  { cmd: 'project tree', desc: 'Show project hierarchy tree' },
  { cmd: 'project siblings', desc: 'List sibling projects' },
  { cmd: 'host add', desc: 'Enrol a satellite host' },
  { cmd: 'host edit', desc: 'Edit satellite watch root' },
  { cmd: 'env install', desc: 'Install environment on a satellite' },
  { cmd: 'where', desc: 'Search satellite paths for a project' },
  { cmd: 'run', desc: 'Run a command on a satellite' },
  { cmd: 'audit-satellite', desc: 'Audit satellite directory for projects' },
  { cmd: 'satellite enroll', desc: 'Enrol a new satellite' },
  { cmd: 'satellite list', desc: 'List enrolled satellites' },
  { cmd: 'satellite remove', desc: 'Remove a satellite from the registry' },
  { cmd: 'mcp connect', desc: 'Connect an MCP server' },
  { cmd: 'mcp disconnect', desc: 'Disconnect an MCP server' },
];

/** Filter commands by a partial input string. */
export function matching(cmds: typeof COMMANDS, partial: string): typeof COMMANDS {
  if (!partial) return cmds;
  const p = partial.toLowerCase();
  return cmds.filter((c) => c.cmd.startsWith(p) || c.cmd.includes(p));
}

export function InputBar({ value, suggestions, selectedIndex }: Props) {
  const dialog = useUIStore((s) => s.dialog);
  const isTextDialog = Boolean(dialog && 'choices' in dialog && dialog.choices && dialog.choices.length === 0);
  const display = value || (isTextDialog ? 'type here and press Enter' : 'Type a message…');
  const showDropdown = suggestions.length > 0;

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
          {suggestions.slice(0, 10).map((s, i) => (
            <Text key={s.cmd} color={i === selectedIndex ? colors.primary : colors.secondary}>
              {i === selectedIndex ? '\u203A ' : '  '}/{s.cmd}  <Text color={colors.textMuted}>{s.desc}</Text>
            </Text>
          ))}
        </Box>
      ) : null}
    </Box>
  );
}
