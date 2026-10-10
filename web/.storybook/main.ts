import type { StorybookConfig } from "@storybook/react-vite";
import { mergeConfig } from "vite";

const allowedHosts = (process.env.OMNIGENT_STORYBOOK_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((host) => host.trim())
  .filter(Boolean);

function shikiManualChunk(id: string): string | undefined {
  const normalized = id.replaceAll("\\", "/");
  if (normalized.includes("/@shikijs/langs/")) return undefined;
  if (normalized.includes("/shiki") || normalized.includes("/@shikijs/")) return "shiki";
  return undefined;
}

const config: StorybookConfig = {
  stories: ["../src/**/*.stories.@(js|jsx|mjs|ts|tsx)"],
  framework: {
    name: "@storybook/react-vite",
    options: {
      builder: {
        viteConfigPath: "vite.storybook.config.ts",
      },
    },
  },
  core: {
    disableTelemetry: true,
    allowedHosts,
  },
  viteFinal: (viteConfig) =>
    mergeConfig(viteConfig, {
      server: { allowedHosts },
      build: {
        rollupOptions: {
          output: {
            manualChunks: shikiManualChunk,
          },
        },
      },
    }),
};

export default config;
