/** Keep the newest advertised GPT-6 variant of each family; preserve other providers. */
export function currentModelChoices<T>(rows: readonly T[], modelId: (row: T) => string): T[] {
  const newest = new Map<string, number>();
  const parse = (row: T) =>
    modelId(row).match(/^(?:(?:openai|codex)\/)?gpt-6(?:\.(\d+))?(?:-(.*))?$/i);
  for (const row of rows) {
    const match = parse(row);
    if (match)
      newest.set(match[2] ?? "", Math.max(newest.get(match[2] ?? "") ?? 0, Number(match[1] ?? 0)));
  }
  return rows.filter((row) => {
    const id = modelId(row);
    const match = parse(row);
    if (match) return Number(match[1] ?? 0) === newest.get(match[2] ?? "");
    return !/^(?:(?:openai|codex)\/)?gpt-/i.test(id);
  });
}
