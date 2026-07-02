import { Box, Text } from 'ink';
import { colors, BAR } from '../theme.js';
import { useUIStore } from '../store/index.js';

export interface CommandDef {
  cmd: string;
  desc: string;
  usage: string;
}

interface Props {
  value: string;
  suggestions: CommandDef[];
  selectedIndex: number;
}

export const COMMANDS: CommandDef[] = [
  { cmd: 'exit', desc: 'Exit the REPL', usage: '/exit' },
  { cmd: 'help', desc: 'Show help', usage: '/help' },
  { cmd: 'context', desc: 'Show current context', usage: '/context' },
  { cmd: 'reset', desc: 'Start a new session', usage: '/reset' },
  { cmd: 'cost', desc: 'Show token usage and cost', usage: '/cost' },
  { cmd: 'model', desc: 'Switch model', usage: '/model [id|number]' },
  { cmd: 'sessions', desc: 'List recent sessions', usage: '/sessions' },
  { cmd: 'no-diff', desc: 'Disable diff approval prompts', usage: '/no-diff' },
  { cmd: 'mode', desc: 'Toggle agent mode (BUILD/PLAN/AUTO)', usage: '/mode [build|plan|auto]' },
  { cmd: 'retry', desc: 'Re-send the last user message', usage: '/retry' },
  { cmd: 'connect', desc: 'Connect an API provider', usage: '/connect [--file <path>]' },
  { cmd: 'project', desc: 'List projects', usage: '/project [id|new <id>|rename|delete]' },
  { cmd: 'project new', desc: 'Create a new project', usage: '/project new <id> [name]' },
  { cmd: 'project rename', desc: 'Rename a project', usage: '/project rename <id> <name>' },
  { cmd: 'project delete', desc: 'Delete a project', usage: '/project delete <id>' },
  { cmd: 'project add-location', desc: 'Add a location to a project', usage: '/project add-location <id>' },
  { cmd: 'project locations', desc: 'List project locations', usage: '/project locations' },
  { cmd: 'project enrich', desc: 'Probe paths for project metadata', usage: '/project enrich' },
  { cmd: 'project tree', desc: 'Show project hierarchy tree', usage: '/project tree' },
  { cmd: 'project siblings', desc: 'List sibling projects', usage: '/project siblings <id>' },
  { cmd: 'host add', desc: 'Enrol a satellite host', usage: '/host add <ssh_host>' },
  { cmd: 'host edit', desc: 'Edit satellite watch root', usage: '/host edit <host_id>' },
  { cmd: 'env install', desc: 'Install environment on a satellite', usage: '/env install <host_id>' },
  { cmd: 'where', desc: 'Search satellite paths for projects', usage: '/where <path>' },
  { cmd: 'run', desc: 'Run a command on a satellite', usage: '/run <host> <command>' },
  { cmd: 'audit-satellite', desc: 'Audit directory for projects', usage: '/audit-satellite' },
  { cmd: 'satellite enroll', desc: 'Enrol a new satellite', usage: '/satellite enroll <ssh_host>' },
  { cmd: 'satellite list', desc: 'List enrolled satellites', usage: '/satellite list' },
  { cmd: 'satellite remove', desc: 'Remove a satellite', usage: '/satellite remove <host_id>' },
  { cmd: 'mcp connect', desc: 'Connect an MCP server', usage: '/mcp connect <server_id>' },
  { cmd: 'mcp disconnect', desc: 'Disconnect an MCP server', usage: '/mcp disconnect <server_id>' },
];

/** Filter commands by a partial input string. */
export function matching(cmds: CommandDef[], partial: string): CommandDef[] {
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
              {i === selectedIndex ? '\u203A ' : '  '}{s.usage}  <Text color={colors.textMuted}>{s.desc}</Text>
            </Text>
          ))}
        </Box>
      ) : null}
    </Box>
  );
}
