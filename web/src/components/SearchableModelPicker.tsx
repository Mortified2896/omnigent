import { useEffect, useState } from "react";
import { ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { MODEL_SELECT_DEFAULT } from "@/components/HarnessConfigControls";
import type { NativeModelOption } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface ModelPickerOption {
  /** Actual model/catalog identifier. */
  id: string;
  displayName: string;
  accessLane?: NativeModelOption["accessLane"];
  groupLabel?: string;
  disabledReason?: string;
  description?: string;
  /** Optional semantic selection key; useful when one option selects model + effort. */
  selectionId?: string;
  keywords?: string[];
}

/** Return the same lane-aware identity the composer has always used. */
export function modelOptionSelectionIdentity(
  option: Pick<ModelPickerOption, "id" | "accessLane" | "selectionId">,
): string {
  return (
    option.selectionId ??
    (option.accessLane ? JSON.stringify([option.accessLane, option.id]) : option.id)
  );
}

export interface SearchableModelPickerProps {
  value: string;
  options: readonly ModelPickerOption[];
  loading: boolean;
  onValueChange: (value: string) => void;
  compact?: boolean;
  disabled?: boolean;
  includeDefault?: boolean;
  defaultValue?: string;
  defaultLabel?: string;
  placeholder?: string;
  id?: string;
  ariaLabel?: string;
  testId?: string;
  searchTestId?: string;
}

export function SearchableModelPicker({
  value,
  options,
  loading,
  onValueChange,
  compact = false,
  disabled = false,
  includeDefault = true,
  defaultValue = MODEL_SELECT_DEFAULT,
  defaultLabel = "Default",
  placeholder = "",
  id,
  ariaLabel = "Model",
  testId = "new-chat-landing-config-model",
  searchTestId = "new-chat-landing-config-model-search",
}: SearchableModelPickerProps) {
  const [open, setOpen] = useState(false);
  const isMobile = useIsMobileViewport();
  const [collisionTop, setCollisionTop] = useState(16);
  useEffect(() => {
    if (!open || !isMobile) return;
    const sync = () => {
      const header = document.querySelector(".chat-header");
      const viewportTop = window.visualViewport?.offsetTop ?? 0;
      setCollisionTop(
        Math.max(16, (header?.getBoundingClientRect().bottom ?? 0) - viewportTop + 8),
      );
    };
    sync();
    window.addEventListener("resize", sync);
    window.visualViewport?.addEventListener("resize", sync);
    window.visualViewport?.addEventListener("scroll", sync);
    return () => {
      window.removeEventListener("resize", sync);
      window.visualViewport?.removeEventListener("resize", sync);
      window.visualViewport?.removeEventListener("scroll", sync);
    };
  }, [open, isMobile]);

  const selectedLabel =
    includeDefault && value === defaultValue
      ? defaultLabel
      : (options.find((option) => modelOptionSelectionIdentity(option) === value)?.displayName ??
        (value || placeholder));
  const select = (nextValue: string) => {
    onValueChange(nextValue);
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          id={id}
          aria-expanded={open}
          aria-label={ariaLabel}
          disabled={disabled}
          className={cn(
            "h-8 justify-between gap-2 px-2.5 font-normal",
            compact ? "w-28 max-w-full sm:w-60" : "w-full",
          )}
          data-testid={testId}
        >
          <span className="min-w-0 flex-1 truncate text-left">{selectedLabel}</span>
          <ChevronDownIcon className="size-4 shrink-0 text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        collisionPadding={{ top: isMobile ? collisionTop : 16, right: 16, bottom: 16, left: 16 }}
        onOpenAutoFocus={(event) => {
          // Opening a touch picker should not summon the keyboard.
          if (isMobile) {
            event.preventDefault();
            if (event.target instanceof HTMLElement) event.target.focus();
          }
        }}
        className="max-h-[min(20rem,var(--radix-popover-content-available-height))] w-[min(20rem,calc(100vw-2rem))] overflow-hidden p-0 md:w-96"
      >
        <Command className="h-auto min-h-0">
          <CommandInput
            placeholder="Search models…"
            aria-label={`Search ${ariaLabel.toLowerCase()}s`}
            className="text-base md:text-ui"
            data-testid={searchTestId}
          />
          <CommandList
            className="max-h-72 min-h-0 overflow-y-auto overscroll-contain"
            onWheel={(event) => event.stopPropagation()}
          >
            {includeDefault ? (
              <CommandItem
                value={defaultValue}
                data-checked={value === defaultValue}
                onSelect={() => select(defaultValue)}
              >
                {defaultLabel}
              </CommandItem>
            ) : null}
            {Array.from(
              options.reduce((groups, option) => {
                const label = option.groupLabel ?? "Models";
                groups.set(label, [...(groups.get(label) ?? []), option]);
                return groups;
              }, new Map<string, (typeof options)[number][]>()),
            ).map(([label, group]) => (
              <CommandGroup
                key={label}
                heading={label}
                className="mt-1 border-t border-border/70 pt-1 **:[[cmdk-group-heading]]:font-semibold **:[[cmdk-group-heading]]:text-foreground"
              >
                {group.map((option) => (
                  <CommandItem
                    key={`${option.accessLane ?? "logical"}:${modelOptionSelectionIdentity(option)}`}
                    value={modelOptionSelectionIdentity(option)}
                    keywords={[
                      option.displayName,
                      option.id,
                      option.groupLabel ?? "",
                      ...(option.keywords ?? []),
                    ]}
                    title={option.disabledReason ?? option.description ?? option.displayName}
                    disabled={Boolean(option.disabledReason)}
                    data-model-id={option.id}
                    data-access-lane={option.accessLane}
                    data-checked={value === modelOptionSelectionIdentity(option)}
                    onSelect={() => select(modelOptionSelectionIdentity(option))}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block">{option.displayName}</span>
                      {(option.disabledReason || option.description) && (
                        <span className="block whitespace-normal text-xs text-muted-foreground">
                          {option.disabledReason ?? option.description}
                        </span>
                      )}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
            {!loading && <CommandEmpty>No models found</CommandEmpty>}
            {loading && (
              <div className="px-2 py-3 text-center text-xs text-muted-foreground">
                Loading models…
              </div>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
