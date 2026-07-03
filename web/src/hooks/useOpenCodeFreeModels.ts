import { useEffect, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";

export interface OpenCodeFreeModel {
  id: string;
  name: string;
  context_limit: number | null;
  output_limit: number | null;
}

interface OpenCodeFreeModelsResponse {
  models: OpenCodeFreeModel[];
  last_synced_at: string | null;
  free_model_count: number;
  error?: string;
}

/**
 * Fetch the validated OpenCode free-model catalog from the server.
 *
 * Returns the list of free models from the local catalog, or an empty
 * list if the catalog is unavailable. Fetched on mount and cached for
 * the component lifetime.
 */
export function useOpenCodeFreeModels(): {
  models: OpenCodeFreeModel[];
  isLoading: boolean;
  error: Error | null;
} {
  const [models, setModels] = useState<OpenCodeFreeModel[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function fetchModels() {
      try {
        const res = await authenticatedFetch("/v1/opencode/free-models");
        if (!res.ok) {
          throw new Error(`Failed to fetch OpenCode free models: ${res.status}`);
        }
        const data: OpenCodeFreeModelsResponse = await res.json();
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
    return () => { cancelled = true; };
  }, []);

  return { models, isLoading, error };
}
