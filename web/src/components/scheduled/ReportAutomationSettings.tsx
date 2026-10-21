import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";

/** Fork-owned settings entry; the upstream settings page only mounts this card. */
export function ReportAutomationSettings() {
  return (
    <section className="flex flex-col gap-3" aria-label="Reports and automations">
      <h2 className="text-ui font-medium">Reports and automations</h2>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card p-4">
        <p className="min-w-0 flex-1 text-ui text-muted-foreground">
          Edit recurring prompts and schedules. Each report also has its own Report settings button.
        </p>
        <Button asChild variant="outline">
          <Link to="/tasks">Manage automations</Link>
        </Button>
      </div>
    </section>
  );
}
