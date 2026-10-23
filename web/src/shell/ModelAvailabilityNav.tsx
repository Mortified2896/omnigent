import { useQuery } from "@tanstack/react-query";
import type { MouseEventHandler } from "react";
import { useLocation } from "@/lib/routing";
import { ListChecksIcon } from "lucide-react";
import { authenticatedFetch } from "@/lib/identity";
import { PrimaryNavLink } from "./PrimaryNavLink";

export function ModelAvailabilityNav({
  onNavigate,
}: {
  onNavigate?: MouseEventHandler<HTMLAnchorElement>;
}) {
  const location = useLocation();
  const status = useQuery({
    queryKey: ["tb4-routing-enabled"],
    staleTime: 60000,
    retry: false,
    queryFn: async () => {
      const response = await authenticatedFetch("/v1/routing/tb4");
      return response.ok ? ((await response.json()) as { enabled: boolean }) : { enabled: false };
    },
  });
  if (!status.data?.enabled) return null;
  return (
    <PrimaryNavLink
      to="/models"
      label="Available models"
      icon={ListChecksIcon}
      active={location.pathname.endsWith("/models")}
      onClick={onNavigate}
      componentId="sidebar.model_availability"
      testId="model-availability-nav"
    />
  );
}
