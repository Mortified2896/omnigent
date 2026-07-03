import { useEffect, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";

/**
 * A normalized model option from the generic harness-model-options endpoint.
 *
 * All harnesses return this same shape so the AgentPicker never needs
 * harness-specific logic.
 */
export interface HarnessModelOption {
  id: string;
  label: string;
  provider: string;
  tier: string;
  kind: string;
  manual_fallback_only: boolean;
  requires_credentials: boolean;
  billing_risk: string;
  context_limit: number | null;
  output_limit: number | null;
}

interface HarnessModelOptionsResponse {
  harness: string;
  source: string | null;
  models: HarnessModelOption[];
  last_synced_at: string | null;
  error?: string;
}

/**
 * Fetch model options for a given canonical harness from the server.
 *
 * Returns the list of available models (e.g. OpenCode free models for
 * ``opencode-native``), or an empty list if the harness is unsupported
 * or the catalog is unavailable. Fetched on mount and cached for the
 * component lifetime.
 *
 * @param harness - Canonical harness id, or ``null`` / ``undefined`` to
 *   skip fetching (returns empty immediately).
 */
export function useHarnessModelOptions(harness: string | null | undefined): {
  models: HarnessModelOption[];
  isLoading: boolean;
  error: Error | null;
} {
  const [models, setModels] = useState<HarnessModelOption[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!harness) {
      setModels([]);
      setIsLoading(false);
      setError(null);
      return;
    }

    // The async closure below can't see TypeScript's narrowing of ``harness``
    // (it's a captured outer variable), so bind the narrowed value to a local
    // before the async hop. Same string either way — the outer ``if (!harness)
    // return`` ensures we never reach here with a null/undefined value.
    const targetHarness: string = harness;
    let cancelled = false;
    setIsLoading(true);
    setError(null);

    async function fetchModels() {
      try {
        const encoded = encodeURIComponent(targetHarness);
        const res = await authenticatedFetch(
          `/v1/harness-model-options?harness=${encoded}`,
        );
        if (!res.ok) {
          throw new Error(
            `Failed to fetch model options for ${targetHarness}: ${res.status}`,
          );
        }
        const data: HarnessModelOptionsResponse = await res.json();
        if (!cancelled) {
          setModels(data.models ?? []);
          setIsLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err : new Error(String(err)));
          setIsLoading(false);
        }
      }
    }

    fetchModels();
    return () => {
      cancelled = true;
    };
  }, [harness]);

  return { models, isLoading, error };
}
