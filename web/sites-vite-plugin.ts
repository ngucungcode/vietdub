import { cp, mkdir, rm } from "node:fs/promises";
import { resolve } from "node:path";
import type { Plugin } from "vite";

// Package the Sites deployment manifest alongside the compiled worker.
export function sites(): Plugin {
  let root = process.cwd();
  return {
    name: "sites",
    apply: "build",
    configResolved(config) {
      root = config.root;
    },
    async closeBundle() {
      const outputDirectory = resolve(root, "dist", ".openai");
      await rm(outputDirectory, { recursive: true, force: true });
      await mkdir(outputDirectory, { recursive: true });
      await cp(
        resolve(root, ".openai", "hosting.json"),
        resolve(outputDirectory, "hosting.json"),
      );
    },
  };
}
