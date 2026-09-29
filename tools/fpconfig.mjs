// Node twin of fpconfig.py: repository root (CODE) and workspace (WORK) from the same config file.
// Config: env FLOORPLAN_CONFIG, else config/project.json, else config/project.example.json.
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, isAbsolute } from 'node:path';

export const CODE = join(dirname(fileURLToPath(import.meta.url)), '..');
const own = join(CODE, 'config/project.json');
export const CONFIG_PATH = process.env.FLOORPLAN_CONFIG || (existsSync(own) ? own : join(CODE, 'config/project.example.json'));
export const CFG = JSON.parse(readFileSync(CONFIG_PATH, 'utf8'));
const abs = (p) => (isAbsolute(p) ? p : join(CODE, p));
export const WORK = abs(CFG.workspace || 'work');

/** value of a "--name value" CLI option, or the default */
export function opt(args, name, dflt) {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? abs(args[i + 1]) : dflt;
}
