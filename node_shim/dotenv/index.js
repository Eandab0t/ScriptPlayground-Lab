"use strict";
/* Offline stub for the `dotenv` npm package: the python host already provides
 * placeholder environment variables, so config() is a no-op. */
module.exports = {
  config: () => ({ parsed: {}, error: null }),
  parse: () => ({}),
};
