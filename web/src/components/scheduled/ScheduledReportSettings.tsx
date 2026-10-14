import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ClockIcon, ListChecksIcon, Loader2Icon, Settings2Icon } from "lucide-react";
import { Link } from "@/lib/routing";
import { getScheduledTask, updateScheduledTask } from "@/lib/scheduledTasksApi";
import { SCHEDULED_TASKS_KEY } from "@/hooks/useScheduledTasks";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

/** Owner-only controls for the automation that produced this conversation. */
export function ScheduledReportSettings({
  taskId,
  onChooseActions,
}: {
  taskId: string;
  onChooseActions?: () => boolean;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState("change");
  const [change, setChange] = useState("");
  const [prompt, setPrompt] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionStatus, setActionStatus] = useState<string | null>(null);
  const basePrompt = useRef<string | null>(null);
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["scheduled-task", taskId],
    queryFn: () => getScheduledTask(taskId),
    enabled: open,
    staleTime: 0,
    retry: false,
  });

  useEffect(() => {
    if (open && query.data && !query.isFetching && basePrompt.current === null) {
      basePrompt.current = query.data.prompt;
      setPrompt(query.data.prompt);
    }
  }, [open, query.data, query.isFetching]);

  function changeOpen(next: boolean) {
    if (saving) return;
    setOpen(next);
    if (next) {
      basePrompt.current = null;
      setMode("change");
      setChange("");
      setSaved(false);
      setError(null);
    }
  }

  async function save() {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      // Read again before adding an instruction, so agent edits made while the
      // dialog was open are retained. Full edits must start from the same prompt.
      const current = await getScheduledTask(taskId);
      if (mode === "prompt" && current.prompt !== basePrompt.current) {
        throw new Error(
          "The prompt changed elsewhere. Reopen settings to load the saved prompt. Your draft is still here.",
        );
      }
      const nextPrompt =
        mode === "change"
          ? `${current.prompt.trimEnd()}\n\nAdditional instructions for future reports:\n${change.trim()}`
          : prompt.trim();
      const updated = await updateScheduledTask(taskId, { prompt: nextPrompt });
      basePrompt.current = updated.prompt;
      setPrompt(updated.prompt);
      setChange("");
      setSaved(true);
      client.setQueryData(["scheduled-task", taskId], updated);
      void client.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save the prompt. Try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div className="mx-auto flex w-full max-w-3xl shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 px-4 pb-2 text-xs text-muted-foreground min-[2561px]:max-w-[clamp(56rem,30vw,64rem)]">
        <span className="flex items-center gap-1.5">
          <ClockIcon className="size-3.5" />
          Ask here to change future reports.
        </span>
        <div className="flex items-center gap-1">
          {onChooseActions && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1.5 px-2 text-xs"
              onClick={() =>
                setActionStatus(
                  onChooseActions()
                    ? "Request added to your draft. Press Send to get action choices."
                    : "Could not prepare the request. Open a writable chat and try again.",
                )
              }
            >
              <ListChecksIcon className="size-3.5" />
              Choose actions
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1.5 px-2 text-xs"
            onClick={() => changeOpen(true)}
          >
            <Settings2Icon className="size-3.5" />
            Report settings
          </Button>
        </div>
        {actionStatus && (
          <p role="status" className="w-full text-xs">
            {actionStatus}
          </p>
        )}
      </div>
      <Dialog open={open} onOpenChange={changeOpen}>
        <DialogContent
          className="flex max-h-[85dvh] flex-col overflow-y-auto sm:max-w-2xl"
          onInteractOutside={(event) => event.preventDefault()}
        >
          <DialogHeader>
            <DialogTitle>{query.data?.name ?? "Report"} settings</DialogTitle>
            <DialogDescription>
              Changes apply to future runs. This report stays available as it is.
            </DialogDescription>
          </DialogHeader>
          {query.isPending && (
            <p role="status" className="text-sm text-muted-foreground">
              Loading saved prompt…
            </p>
          )}
          {query.isError && (
            <div role="alert" className="flex items-center justify-between gap-3 text-sm">
              <span>
                Could not load this automation. It may have been removed or you may no longer have
                access.
              </span>
              <Button variant="outline" onClick={() => void query.refetch()}>
                Retry
              </Button>
            </div>
          )}
          {query.data && !query.isError && (
            <Tabs
              value={mode}
              onValueChange={(value) => {
                setMode(value);
                setSaved(false);
                setError(null);
              }}
            >
              <TabsList>
                <TabsTrigger value="change">Request a change</TabsTrigger>
                <TabsTrigger value="prompt">Edit full prompt</TabsTrigger>
              </TabsList>
              <TabsContent value="change" className="flex flex-col gap-2">
                <label htmlFor={`report-change-${taskId}`} className="text-sm font-medium">
                  What should future reports do differently?
                </label>
                <p className="text-sm text-muted-foreground">
                  Your instruction will be added to the saved prompt. You can also ask for changes
                  in the conversation below the report.
                </p>
                <Textarea
                  id={`report-change-${taskId}`}
                  value={change}
                  onChange={(event) => {
                    setChange(event.target.value);
                    setSaved(false);
                  }}
                  disabled={saving}
                  style={{ fieldSizing: "fixed" }}
                  rows={5}
                  placeholder="Please add a section about Codex resets: when the next one is due and whether one happened overnight."
                />
              </TabsContent>
              <TabsContent value="prompt" className="flex flex-col gap-2">
                <label htmlFor={`report-prompt-${taskId}`} className="text-sm font-medium">
                  Saved report prompt
                </label>
                <Textarea
                  id={`report-prompt-${taskId}`}
                  value={prompt}
                  onChange={(event) => {
                    setPrompt(event.target.value);
                    setSaved(false);
                  }}
                  disabled={saving}
                  style={{ fieldSizing: "fixed" }}
                  className="h-[min(40dvh,24rem)] resize-none overflow-y-auto text-sm"
                  rows={12}
                />
              </TabsContent>
            </Tabs>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          {saved && (
            <p role="status" className="text-sm">
              Saved for future runs.
            </p>
          )}
          <DialogFooter className="gap-2 sm:justify-between">
            <Button asChild variant="ghost">
              <Link to="/tasks">Manage automations</Link>
            </Button>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => changeOpen(false)} disabled={saving}>
                {saved ? "Done" : "Cancel"}
              </Button>
              <Button
                onClick={() => void save()}
                disabled={
                  saving ||
                  !query.data ||
                  query.isError ||
                  basePrompt.current === null ||
                  (mode === "change"
                    ? !change.trim()
                    : !prompt.trim() || prompt === basePrompt.current)
                }
              >
                {saving && <Loader2Icon className="size-4 animate-spin" />}
                {saving ? "Saving…" : mode === "change" ? "Save change" : "Save prompt"}
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
