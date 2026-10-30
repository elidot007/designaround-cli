/**
 * Isolation of the embedded Pi runtime. Imported FIRST by main.ts so these
 * variables are set before any Pi module is evaluated.
 *
 * - Pi's config dir is $DESIGNAROUND_HOME/pi-agent, never ~/.pi/agent.
 * - No network catalog refresh, no version check, no install telemetry.
 * - Sessions are in-memory; settings are in-memory; no resource discovery from
 *   ~/.pi or <cwd>/.pi (see runtime.ts: custom ResourceLoader, noTools builtin).
 */
import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const DESIGNAROUND_HOME = process.env.DESIGNAROUND_HOME || join(homedir(), ".designaround");
export const PI_AGENT_DIR = join(DESIGNAROUND_HOME, "pi-agent");

mkdirSync(PI_AGENT_DIR, { recursive: true, mode: 0o700 });
process.env.PI_CODING_AGENT_DIR = PI_AGENT_DIR;
process.env.PI_OFFLINE = "1";
process.env.PI_SKIP_VERSION_CHECK = "1";
process.env.PI_TELEMETRY = "0";
