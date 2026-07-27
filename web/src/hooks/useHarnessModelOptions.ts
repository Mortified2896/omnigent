import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface HarnessModelOption {
  id: string;
  label: string;
  provider: string;
  source?: string;
  provider_id?: string;
  access_source?: string;
  availability?: string;
  route_id?: string;
  reasoning_efforts?: string[];
  variants?: string[];
}

export interface HarnessModelOptionGroup {
  label?: string;
  source?: string;
  error?: string | null;
  models: HarnessModelOption[];
}

export interface HarnessModelOptions {
  groups: HarnessModelOptionGroup[];
  models: HarnessModelOption[];
}

function stringList(value: unknown): string[] | undefined {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) return undefined;
  return value;
}

function parseModel(value: unknown): HarnessModelOption | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  if (
    typeof row.id !== "string" ||
    typeof row.label !== "string" ||
    typeof row.provider !== "string"
  ) {
    return null;
  }
  return {
    id: row.id,
    label: row.label,
    provider: row.provider,
    source: typeof row.source === "string" ? row.source : undefined,
    provider_id: typeof row.provider_id === "string" ? row.provider_id : undefined,
    access_source: typeof row.access_source === "string" ? row.access_source : undefined,
    availability: typeof row.availability === "string" ? row.availability : undefined,
    route_id: typeof row.route_id === "string" ? row.route_id : undefined,
    reasoning_efforts: stringList(row.reasoning_efforts),
    variants: stringList(row.variants),
  };
}

function parseGroup(value: unknown): HarnessModelOptionGroup | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const group = value as Record<string, unknown>;
  return {
    label: typeof group.label === "string" ? group.label : undefined,
    source: typeof group.source === "string" ? group.source : undefined,
    error: typeof group.error === "string" ? group.error : null,
    models: Array.isArray(group.models) ? group.models.map(parseModel).filter(Boolean) : [],
  } as HarnessModelOptionGroup;
}

async function fetchHarnessModelOptions(harness: string): Promise<HarnessModelOptions> {
  const res = await authenticatedFetch(
    `/v1/harness-model-options?harness=${encodeURIComponent(harness)}`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as Record<string, unknown>;
  const groups = (
    Array.isArray(body.groups) ? body.groups.map(parseGroup).filter(Boolean) : []
  ) as HarnessModelOptionGroup[];
  const flatModels = (
    Array.isArray(body.models) ? body.models.map(parseModel).filter(Boolean) : []
  ) as HarnessModelOption[];
  return {
    models: flatModels.length > 0 ? flatModels : groups.flatMap((group) => group.models),
    groups,
  };
}

/** Read the direct model catalog exposed by a native harness. */
export function useHarnessModelOptions(harness: string | null) {
  return useQuery({
    queryKey: ["harness-model-options", harness],
    queryFn: () => fetchHarnessModelOptions(harness!),
    enabled: harness != null,
    staleTime: 30_000,
  });
}
