import { useState } from "react";
import { RotateCcwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import type {
  O3ProposalAdjustment,
  O3RequirementOverrides,
  O3RoutingProposal,
} from "@/lib/o3RoutingReview";

const toggles = [
  [
    "tools",
    "Tools required",
    "Task requires external tools such as repository, shell, MCP, GitHub, or connected capabilities. Off means not required; it does not disable tools.",
  ],
  ["image_input", "Image input", "Model must be able to inspect images supplied as task input."],
  [
    "image_output",
    "Image generation",
    "Model must produce image output. Images returned by tools do not establish this capability.",
  ],
  [
    "structured_output",
    "Structured output",
    "Execution requires reliable structured/schema-constrained output.",
  ],
] as const;

export function ExecutionProfile({
  proposal,
  disabled,
  onAdjust,
}: {
  proposal: O3RoutingProposal;
  disabled: boolean;
  onAdjust: (adjustment: O3ProposalAdjustment) => Promise<void>;
}) {
  const requirements = proposal.effective_requirements ?? proposal.adviser.requirements;
  const overrides = proposal.requirement_overrides ?? {};
  const values = {
    tools: requirements.tools,
    image_input: requirements.vision || (requirements.input_modalities ?? []).includes("image"),
    image_output: (requirements.output_modalities ?? []).includes("image"),
    structured_output: requirements.structured_output ?? false,
  };
  const [context, setContext] = useState(String(requirements.minimum_context_tokens));
  const change = (field: keyof O3RequirementOverrides, value: boolean | number | null) =>
    void onAdjust({ requirement_overrides: { [field]: value } });
  const reset = (field: keyof O3RequirementOverrides, label: string) =>
    overrides[field] != null ? (
      <span
        className="flex items-center gap-1 text-xs text-muted-foreground"
        data-testid={`o3-override-${field}`}
      >
        Overridden
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-7"
          disabled={disabled}
          aria-label={`Reset ${label} to estimator`}
          onClick={() => change(field, null)}
        >
          <RotateCcwIcon className="size-3.5" />
        </Button>
      </span>
    ) : null;
  const numeric = (
    field: "minimum_context_tokens" | "minimum_output_tokens",
    label: string,
    value: string,
    setValue: (value: string) => void,
  ) => {
    const parsed = Number(value);
    const valid = value.trim() !== "" && Number.isSafeInteger(parsed) && parsed >= 0;
    return (
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex flex-1 items-center justify-between gap-3 text-sm">
          {label}
          <input
            type="number"
            min="0"
            step="1"
            value={value}
            disabled={disabled}
            aria-label={label}
            aria-invalid={!valid}
            data-testid={`o3-capability-${field}`}
            className="h-8 w-32 rounded-md border border-input bg-background px-2 text-sm"
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || !valid || parsed === (requirements[field] ?? 0)}
          onClick={() => change(field, parsed)}
          aria-label={`Apply ${label}`}
        >
          Apply
        </Button>
        {reset(field, label)}
      </div>
    );
  };
  return (
    <section
      className="space-y-3 rounded-lg border border-border p-3"
      data-testid="o3-execution-profile"
    >
      <div>
        <h3 className="text-sm font-semibold">Execution profile</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          The estimator sets these task requirements. Changes recheck routes before approval.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {toggles.map(([field, label, description]) => (
          <div key={field} className="space-y-1">
            <div className="flex items-center justify-between gap-2">
              <label htmlFor={`o3-capability-${field}`} className="flex-1 text-sm">
                {label}
              </label>
              {reset(field, label)}
              <Switch
                id={`o3-capability-${field}`}
                checked={values[field]}
                disabled={disabled}
                aria-describedby={`o3-capability-help-${field}`}
                data-testid={`o3-capability-${field}`}
                onCheckedChange={(checked) => change(field, checked)}
              />
            </div>
            <p id={`o3-capability-help-${field}`} className="text-xs text-muted-foreground">
              {description}
            </p>
          </div>
        ))}
      </div>
      {numeric("minimum_context_tokens", "Minimum context (tokens)", context, setContext)}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">Advanced</summary>
        <div className="mt-2 space-y-2">
          <p>
            Zero adds no capacity minimum. Required capabilities with unknown support are excluded.
          </p>
          <p>
            Tools remain available in native sessions; tool-search qualification is checked again at
            approval.
          </p>
        </div>
      </details>
    </section>
  );
}
