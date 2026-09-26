// Scheduled native-harness model controls. Provider model choices come from
// the selected host's runner-owned catalog; Claude keeps its existing alias
// fallback when a host is not pinned.

import { Label } from "@/components/scheduled/Label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  CLAUDE_NATIVE_EFFORTS,
  EFFORT_SELECT_NONE,
  MODEL_SELECT_DEFAULT,
} from "@/components/HarnessConfigControls";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { CLAUDE_NATIVE_PERMISSION_MODES } from "@/lib/claudePermissionMode";
import { useHostModelOptions } from "@/hooks/useHosts";

const PERMISSION_SELECT_DEFAULT = "__agent_default__";
const SEARCH_SELECT_DEFAULT = "__inherit__";
const SEARCH_MODES = [
  { id: "live", label: "Live" },
  { id: "cached", label: "Cached" },
  { id: "indexed", label: "Indexed" },
  { id: "disabled", label: "Disabled" },
] as const;

function effortLabel(value: string): string {
  return value.length === 0 ? value : `${value[0].toUpperCase()}${value.slice(1)}`;
}

export function ModelEffortFields({
  model,
  effort,
  permissionMode,
  webSearchMode,
  hostId,
  harness,
  showPermissionMode,
  onModelChange,
  onEffortChange,
  onPermissionModeChange,
  onWebSearchModeChange,
  onSelectOpenChange,
}: {
  model: string;
  effort: string;
  permissionMode: string;
  webSearchMode: string;
  hostId: string;
  harness: string;
  showPermissionMode: boolean;
  onModelChange: (model: string) => void;
  onEffortChange: (effort: string) => void;
  onPermissionModeChange: (mode: string) => void;
  onWebSearchModeChange: (mode: string) => void;
  onSelectOpenChange?: (open: boolean) => void;
}) {
  const isClaude = harness === "claude-native";
  const isCodex = harness === "codex-native";
  const { data: hostModelOptions } = useHostModelOptions(
    hostId === "" ? null : hostId,
    harness,
    hostId !== "" && !isClaude,
  );
  const liveOptions = hostModelOptions ?? [];
  const modelOptions = isClaude
    ? liveOptions.length > 0
      ? liveOptions.map((option) => ({ id: option.id, label: option.displayName ?? option.id }))
      : CLAUDE_NATIVE_MODELS.map((option) => ({ id: option.id, label: option.label }))
    : liveOptions.map((option) => ({ id: option.id, label: option.displayName ?? option.id }));
  const selectedOption =
    liveOptions.find((option) => option.id === model) ??
    liveOptions.find((option) => option.isDefault) ??
    liveOptions[0];
  const effortOptions = isClaude
    ? CLAUDE_NATIVE_EFFORTS
    : (selectedOption?.supportedReasoningEfforts ?? []).map((item) => ({
        value: item.reasoningEffort,
        label: effortLabel(item.reasoningEffort),
      }));

  return (
    <div className="flex flex-col gap-3 sm:gap-4">
      <div className="grid gap-3 sm:grid-cols-2 sm:gap-6" data-testid="task-model-effort-row">
        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-model-control">
          <Label htmlFor="task-model">Model</Label>
          <Select
            value={model === "" ? MODEL_SELECT_DEFAULT : model}
            componentId="tasks.scheduled.model"
            valueHasNoPii
            onValueChange={(value) => {
              const nextModel = value === MODEL_SELECT_DEFAULT ? "" : value;
              onModelChange(nextModel);
              const next = liveOptions.find((option) => option.id === nextModel);
              if (
                next &&
                effort &&
                !next.supportedReasoningEfforts?.some((item) => item.reasoningEffort === effort)
              ) {
                onEffortChange("");
              }
            }}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger id="task-model" data-testid="task-model-trigger" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="start"
              className="w-(--radix-select-trigger-width)"
            >
              <SelectItem value={MODEL_SELECT_DEFAULT}>Default</SelectItem>
              {modelOptions.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-effort-control">
          <Label htmlFor="task-effort">Effort</Label>
          <Select
            value={effort === "" ? EFFORT_SELECT_NONE : effort}
            componentId="tasks.scheduled.effort"
            valueHasNoPii
            onValueChange={(value) => onEffortChange(value === EFFORT_SELECT_NONE ? "" : value)}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger id="task-effort" data-testid="task-effort-trigger" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="start"
              className="w-(--radix-select-trigger-width)"
            >
              <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
              {effortOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {isCodex && (
        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-web-search-control">
          <Label htmlFor="task-web-search">Web search</Label>
          <Select
            value={webSearchMode || SEARCH_SELECT_DEFAULT}
            componentId="tasks.scheduled.codex_web_search_mode"
            valueHasNoPii
            onValueChange={(value) =>
              onWebSearchModeChange(value === SEARCH_SELECT_DEFAULT ? "" : value)
            }
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger
              id="task-web-search"
              data-testid="task-web-search-trigger"
              className="w-full"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="start"
              className="w-(--radix-select-trigger-width)"
            >
              <SelectItem value={SEARCH_SELECT_DEFAULT}>Default</SelectItem>
              {SEARCH_MODES.map((mode) => (
                <SelectItem key={mode.id} value={mode.id}>
                  {mode.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      {showPermissionMode && (
        <div className="flex w-full min-w-0 flex-col gap-1.5" data-testid="task-permission-control">
          <Label htmlFor="task-permission">Permission mode</Label>
          <Select
            value={permissionMode === "" ? PERMISSION_SELECT_DEFAULT : permissionMode}
            componentId="tasks.scheduled.permission_mode"
            valueHasNoPii
            onValueChange={(value) =>
              onPermissionModeChange(value === PERMISSION_SELECT_DEFAULT ? "" : value)
            }
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger
              id="task-permission"
              data-testid="task-permission-trigger"
              className="w-full"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="start"
              className="w-(--radix-select-trigger-width)"
            >
              <SelectItem value={PERMISSION_SELECT_DEFAULT}>Default</SelectItem>
              {CLAUDE_NATIVE_PERMISSION_MODES.map((mode) => (
                <SelectItem key={mode.value} value={mode.value}>
                  {mode.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  );
}
