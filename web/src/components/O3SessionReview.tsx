import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getO3SessionReviews, getO3FailedReview } from "@/lib/o3RoutingReview";
import { O3DecisionSummary, RawAudit } from "./O3DecisionInspect";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "./ui/dialog";
import { Button } from "./ui/button";

export function O3SessionReview({ sessionId }: { sessionId: string | undefined }) {
  const info = useServerInfo();
  const query = useQuery({
    queryKey: ["o3-session-reviews", sessionId],
    queryFn: () => getO3SessionReviews(sessionId!),
    enabled: !!sessionId && info !== "loading" && info.o3_routing_review_enabled,
    refetchOnWindowFocus: true,
    refetchInterval: 15000,
  });
  if (query.isError)
    return (
      <Button variant="ghost" onClick={() => void query.refetch()}>
        Retry loading O3 review
      </Button>
    );
  return (
    <div className="contents">
      {query.data?.map((p) => (
        <div key={p.proposal_id} className="rounded-lg border px-3 py-2">
          <O3DecisionSummary proposal={p} />
        </div>
      ))}
    </div>
  );
}

export function O3FailedReview({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ["o3-failed-review", id],
    queryFn: () => getO3FailedReview(id),
    enabled: open,
  });
  return (
    <>
      <Button variant="outline" className="min-h-11" onClick={() => setOpen(true)}>
        Inspect failed reviewer output
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90dvh] overflow-auto sm:max-w-4xl">
          <DialogTitle>Reviewer attempts</DialogTitle>
          <DialogDescription>
            No recommendation was accepted. Safe input and complete returned output remain recorded.
          </DialogDescription>
          {query.isError ? (
            <Button onClick={() => void query.refetch()}>Retry</Button>
          ) : (
            <RawAudit label="Failed review capture" value={query.data} />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
