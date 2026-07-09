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
            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground sm:gap-2 sm:px-2 md:h-8"
            data-testid="route-approval-control"
            data-mode={enabled ? "agent" : "manual"}
            title={description}
          >
            <span className="route-approval-short-label whitespace-nowrap font-medium text-foreground sm:hidden">
              Routing
            </span>
            <span className="route-approval-full-label hidden whitespace-nowrap font-medium text-foreground sm:inline">
              Model Routing
            </span>
            <span className="route-approval-mode-label hidden whitespace-nowrap tabular-nums sm:inline">
              {modeLabel}
            </span>
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
