"use strict";
/* Node runner for ScriptPlayground offline bot projects.
 *
 * Usage: node runner.js <absolute-entry.js>
 * NODE_PATH (set by the python host) points at the bundled shims so bare
 * requires like "discord.js" and "dotenv" resolve without any npm install.
 * All communication with the simulator host happens over stdio as NDJSON.
 */

process.env.NODE_PATH = process.env.SHIM_DIR || "";
require("module").Module._initPaths();

const path = require("path");

const entry = process.argv[2];
if (!entry) {
  process.stderr.write("runner.js: missing entry file\n");
  process.exit(2);
}

try {
  require(path.resolve(entry));
} catch (error) {
  process.stdout.write(JSON.stringify({
    type: "boot_error",
    message: String((error && error.stack) || error),
  }) + "\n");
  process.exit(1);
}
