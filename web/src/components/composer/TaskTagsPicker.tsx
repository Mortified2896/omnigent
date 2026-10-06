import { useState } from "react";
import { CheckIcon, TagsIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

const taskTags = ["UI", "Debugging", "Research", "Testing", "Documentation", "Refactoring"];

export function TaskTagsPicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (tags: string[]) => void;
}) {
  const [custom, setCustom] = useState("");
  const available = [...new Set([...taskTags, ...value])];
  function addCustom() {
    const tag = custom.trim();
    if (!tag || value.length >= 8) return;
    if (!value.some((entry) => entry.toLowerCase() === tag.toLowerCase()))
      onChange([...value, tag]);
    setCustom("");
  }
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 shrink-0 gap-1 px-2 text-xs"
          aria-label="Task tags"
        >
          <TagsIcon className="size-3.5" />
          Task tags{value.length > 0 && <span className="tabular-nums"> · {value.length}</span>}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] p-3">
        <p className="text-sm font-medium">Task tags</p>
        <p className="text-xs text-muted-foreground">
          Optional labels for this prompt. Saved with your message for filtering and analysis.
        </p>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Task tag choices">
          {available.map((tag) => (
            <Button
              key={tag}
              variant="outline"
              className={
                value.includes(tag)
                  ? "border-primary bg-primary/15 text-foreground ring-1 ring-primary/50 hover:bg-primary/25 dark:border-primary dark:bg-primary/15 dark:hover:bg-primary/25"
                  : undefined
              }
              size="sm"
              disabled={!value.includes(tag) && value.length >= 8}
              aria-pressed={value.includes(tag)}
              onClick={() =>
                onChange(
                  value.includes(tag) ? value.filter((entry) => entry !== tag) : [...value, tag],
                )
              }
            >
              <CheckIcon
                aria-hidden="true"
                className={value.includes(tag) ? "size-3.5 text-primary" : "invisible size-3.5"}
              />
              {tag}
            </Button>
          ))}
        </div>
        <div className="flex min-w-0 gap-2">
          <Input
            className="min-w-0"
            aria-label="Custom task tag"
            placeholder="Custom tag"
            maxLength={40}
            value={custom}
            onChange={(event) => setCustom(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                addCustom();
              }
            }}
          />
          <Button
            size="sm"
            variant="outline"
            disabled={!custom.trim() || value.length >= 8}
            onClick={addCustom}
          >
            Add tag
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

export function TaskTagsControls({
  value,
  onChange,
}: {
  value: string[];
  onChange: (tags: string[]) => void;
}) {
  return (
    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
      <TaskTagsPicker value={value} onChange={onChange} />
      {value.map((tag) => (
        <button
          key={tag}
          type="button"
          aria-label={`Remove task tag ${tag}`}
          onClick={() => onChange(value.filter((t) => t !== tag))}
          className="inline-flex max-w-full items-center gap-1 rounded border border-primary/60 bg-primary/15 px-2 py-0.5 text-xs text-foreground"
        >
          <span className="truncate">{tag}</span>
          <XIcon className="size-3 shrink-0" />
        </button>
      ))}
    </div>
  );
}
