import { Switch } from "@/components/ui/switch";

export interface RouteApprovalControlProps {
  enabled: boolean;
  disabled?: boolean;
  onChange: (enabled: boolean) => void;
}

export function RouteApprovalControl({ enabled, disabled, onChange }: RouteApprovalControlProps) {
  return (
    <label
      className="flex items-center justify-between gap-3 rounded border px-3 py-2 text-sm"
      data-testid="route-approval-control"
    >
      <span className="flex flex-col gap-0.5">
        <span className="font-medium">Model Routing Agent</span>
        <span className="text-xs text-muted-foreground">
          {enabled
            ? "Router recommends the harness, native OmniRoute route, reasoning effort, and permission mode before execution."
            : "Off: manual harness, model/route, and reasoning selections are preserved."}
        </span>
      </span>
      <Switch
        checked={enabled}
        disabled={disabled}
        onCheckedChange={onChange}
        aria-label="Model Routing Agent"
      />
    </label>
  );
}
