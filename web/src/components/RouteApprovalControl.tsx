import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export interface RouteApprovalControlProps {
  enabled: boolean;
  disabled?: boolean;
  onChange: (enabled: boolean) => void;
}

export function RouteApprovalControl({ enabled, disabled, onChange }: RouteApprovalControlProps) {
  const modeLabel = enabled ? "Agent" : "Manual";
  const description = enabled
    ? "Router recommends the harness, native OmniRoute route, reasoning effort, and permission mode before execution."
    : "Manual harness, model/route, and reasoning selections are preserved.";

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            className="inline-flex h-7 shrink-0 items-center gap-2 rounded-md px-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground md:h-8"
            data-testid="route-approval-control"
            data-mode={enabled ? "agent" : "manual"}
            title={description}
          >
            <span className="whitespace-nowrap font-medium text-foreground">Model Routing</span>
            <span className="whitespace-nowrap tabular-nums">{modeLabel}</span>
            <Switch
              size="sm"
              checked={enabled}
              disabled={disabled}
              onCheckedChange={onChange}
              aria-label="Model Routing Agent"
            />
          </div>
        </TooltipTrigger>
        <TooltipContent side="top" sideOffset={6} className="max-w-80 px-3 py-2 text-xs">
          {description}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
