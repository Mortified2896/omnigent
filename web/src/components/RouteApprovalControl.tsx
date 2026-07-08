import { ShieldCheckIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export type RouteApprovalMode = "on" | "off" | null;

export interface RouteApprovalControlProps {
  value: RouteApprovalMode;
  onChange: (mode: RouteApprovalMode) => void;
  disabled?: boolean;
  serverEnabled: boolean;
}

/**
 * Per-session route-approval toggle. Mirrors the cost-routing toggle
 * UX: a binary glyph button with a tooltip explaining the feature.
 *
 * The server capability probe (``serverEnabled``) drives whether the
 * button is interactive. When the server-level gate is off, the
 * button renders disabled with a tooltip explaining the global
 * toggle is required.
 */
export function RouteApprovalControl({
  value,
  onChange,
  disabled = false,
  serverEnabled,
}: RouteApprovalControlProps) {
  const isOn = value === "on";
  const globallyDisabled = !serverEnabled;

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            disabled={disabled || globallyDisabled}
            aria-label="Route approval gate"
            aria-pressed={isOn}
            data-testid="route-approval-toggle"
            data-mode={isOn ? "on" : "off"}
            data-server-enabled={serverEnabled ? "true" : "false"}
            className="relative size-9 text-muted-foreground hover:bg-transparent dark:hover:bg-transparent md:size-8"
            onClick={() => onChange(isOn ? "off" : "on")}
          >
            <ShieldCheckIcon className="size-4" aria-hidden="true" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" sideOffset={6} className="max-w-56 px-3 py-2 text-xs">
          {globallyDisabled ? (
            <span>
              Route approval gate is not enabled on this Omnigent server. Set{" "}
              <code>OMNIGENT_ROUTE_APPROVAL_GATE=1</code> to enable it.
            </span>
          ) : (
            <span>
              Route approval / automatic routing.
              {isOn
                ? " Before each task, an Execution Route Proposal card appears for approve/modify/decline."
                : " Off — the task runs without pausing for route approval."}
            </span>
          )}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
