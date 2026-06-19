#!/usr/bin/env node

import { stdin, stderr, stdout } from "node:process";
import { runCli } from "../../gateway/src/cli/run";

const controller = new AbortController();
process.on("SIGINT", () => controller.abort());

const exitCode = await runCli(
  process.argv.slice(2),
  {
    stdout(value) {
      stdout.write(`${value}\n`);
    },
    stderr(value) {
      stderr.write(value);
    },
    async readStdin() {
      const chunks: Buffer[] = [];
      for await (const chunk of stdin) {
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      }
      return Buffer.concat(chunks);
    },
  },
  undefined,
  controller.signal,
);
process.exitCode = exitCode;
