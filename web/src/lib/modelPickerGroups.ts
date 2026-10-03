// Provider/transport grouping for lane-aware model pickers.
//
// The host stamps every catalog row that rides an explicit access lane with
// `accessLane` (the machine transport) and `groupLabel` (the provider family
// it belongs to, e.g. "GLM"). One flat list makes the user scan a long
// provider-qualified string per row; these helpers turn the same rows into
// labeled, non-selectable sections so the heading carries the transport and
// each row shows only the model.

import type { NativeModelOption } from "./types";

/** Friendly transport label for a lane id, matching the host's stamping. */
const LANE_LABELS: Readonly<Record<string, string>> = {
  omniroute: "OmniRoute",
  "codex-direct": "Codex Subscription — Direct",
  "glm-direct": "Z.AI Direct",
};

/** One rendered provider/transport section of a model picker. */
export interface ModelPickerGroup {
  /** Grouping key: the row's `accessLane`, or "" for lane-less rows. */
  key: string;
  /** Section heading, or `null` for lane-less rows (no header rendered). */
  label: string | null;
  options: NativeModelOption[];
}

/**
 * The section heading for a lane: the lane's transport label, qualified with
 * the provider family when the family differs from the transport itself
 * (GLM rows ride both the OmniRoute and Z.AI Direct lanes, so "GLM" alone
 * would name two indistinguishable groups).
 */
export function modelGroupLabel(option: NativeModelOption): string | null {
  const lane = option.accessLane;
  if (!lane) return null;
  const laneLabel = LANE_LABELS[lane] ?? lane;
  const family = option.groupLabel;
  return family && family !== laneLabel ? `${family} · ${laneLabel}` : laneLabel;
}

/**
 * Drop a lane suffix the host may have baked into a row's display name
 * ("GLM 4.5 · OmniRoute"). The section heading carries the transport, so a
 * row that repeats it renders the provider string twice. Only a suffix that
 * names the row's own lane/family is stripped — never an unrelated "·" part
 * of a genuine model name.
 */
export function stripModelLaneSuffix(label: string, option: NativeModelOption): string {
  if (!option.accessLane) return label;
  const laneLabel = LANE_LABELS[option.accessLane] ?? option.accessLane;
  const suffixes = new Set(
    [laneLabel, option.groupLabel, option.accessLane].filter(
      (value): value is string => typeof value === "string" && value.length > 0,
    ),
  );
  const separator = " · ";
  const index = label.lastIndexOf(separator);
  if (index < 0) return label;
  return suffixes.has(label.slice(index + separator.length)) ? label.slice(0, index) : label;
}

/** The row label a grouped picker renders: model name only. */
export function groupedModelLabel(
  option: NativeModelOption,
  fallback: (option: NativeModelOption) => string,
): string {
  return stripModelLaneSuffix(fallback(option), option);
}

/**
 * Split picker rows into provider/transport groups, in first-appearance
 * order (the host already orders rows by lane). Lane-less rows — a plain
 * Claude catalog or a session's own unqualified snapshot — form a single
 * header-less group so those pickers render exactly as before.
 */
export function groupModelOptions(options: readonly NativeModelOption[]): ModelPickerGroup[] {
  const groups: ModelPickerGroup[] = [];
  const byKey = new Map<string, ModelPickerGroup>();
  for (const option of options) {
    // Lane + family: GLM rides both the gateway and the direct lane, so a
    // lane-only key would merge two differently-billed "GLM" sections.
    const key = `${option.accessLane ?? ""}|${option.groupLabel ?? ""}`;
    let group = byKey.get(key);
    if (group === undefined) {
      const rendered: ModelPickerGroup = {
        key: option.accessLane ?? "",
        label: modelGroupLabel(option),
        options: [],
      };
      byKey.set(key, rendered);
      groups.push(rendered);
      group = rendered;
    }
    group.options.push(option);
  }
  return groups;
}
