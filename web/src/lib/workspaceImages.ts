/** Raster images may be read only through the conversation's filesystem API. */
function hasControlCharacters(value: string): boolean {
  return Array.from(value).some((character) => character.charCodeAt(0) < 32);
}

export function workspaceImagePath(value: unknown): string | null {
  if (typeof value !== "string" || !value || hasControlCharacters(value) || /[\\?#]/.test(value))
    return null;
  if (/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(value)) return null;
  const parts = value.split("/");
  for (let i = value.startsWith("/") ? 1 : 0; i < parts.length; i += 1) {
    let part: string;
    try {
      part = decodeURIComponent(parts[i]!);
    } catch {
      return null;
    }
    if (
      !part ||
      part === "." ||
      part === ".." ||
      hasControlCharacters(part) ||
      /[/\\?#]/.test(part)
    )
      return null;
    parts[i] = part;
  }
  const path = parts.join("/");
  return /\.(?:png|jpe?g|gif|webp|avif|bmp)$/i.test(path) ? path : null;
}

/** Native Codex already persists image paths; don't turn image bytes into prose. */
export function nativeToolImagePath(
  name: string,
  args: Record<string, unknown>,
  output: string | null,
): string | null {
  if (output === null) return null;
  if (name === "view_image") return workspaceImagePath(args.path);
  if (name !== "generate_image" || !/^status: completed(?:\r?\n|$)/.test(output)) return null;
  const saved = output.match(/^saved to (.+)$/m)?.[1];
  return workspaceImagePath(saved);
}
