import type { ReactNode } from "react";
import { DropdownMenuCheckboxItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { PickerSectionHeader } from "./HarnessMenuRow";

/** One checkbox row in a Models or Effort section. */
export interface ComposerConfigChoice {
  key: string;
  label: ReactNode;
  checked: boolean;
  disabled?: boolean;
  // Omitted for a static, non-selectable row (e.g. the disabled "(current)"
  // model). When present it drives the checkbox's change handler.
  onSelect?: () => void;
  testId?: string;
  title?: string;
  className?: string;
  // Extra data-* attributes (e.g. data-model-id / data-effort-level).
  data?: Record<string, string | undefined>;
}

/**
 * One provider/transport subgroup of a Models section: a non-selectable
 * heading (or none, for lane-less rows) followed by that group's choices.
 */
export interface ComposerConfigChoiceGroup {
  key: string;
  label: string | null;
  choices: ComposerConfigChoice[];
}

/** A single labeled section (Models or Effort) of the harness config menu. */
export interface ComposerConfigSection {
  testId: string;
  header: ReactNode;
  // Rendered between the header and the choices — e.g. a model search box or a
  // loading/empty note. Page-local because it varies per surface.
  leading?: ReactNode;
  choices: ComposerConfigChoice[];
  // Provider/transport groups (host-stamped access lanes). When present the
  // rows render under non-selectable group headings instead of one flat list;
  // `choices` remains the full ordered set for callers that count or index.
  groups?: readonly ComposerConfigChoiceGroup[];
}

function ConfigChoices({ choices }: { choices: readonly ComposerConfigChoice[] }) {
  return (
    <>
      {choices.map((choice) => (
        <DropdownMenuCheckboxItem
          key={choice.key}
          checked={choice.checked}
          disabled={choice.disabled}
          onSelect={(event) => event.preventDefault()}
          onCheckedChange={choice.onSelect ? () => choice.onSelect?.() : undefined}
          data-testid={choice.testId}
          title={choice.title}
          className={choice.className}
          {...choice.data}
        >
          {choice.label}
        </DropdownMenuCheckboxItem>
      ))}
    </>
  );
}

function ConfigGroupedChoices({ groups }: { groups: readonly ComposerConfigChoiceGroup[] }) {
  return (
    <>
      {groups.map((group, index) => (
        <div key={group.key} data-model-group={group.key}>
          {group.label !== null && (
            <PickerSectionHeader data-model-group-label={group.key}>
              {group.label}
            </PickerSectionHeader>
          )}
          <ConfigChoices choices={group.choices} />
          {index < groups.length - 1 && <div className="h-1" aria-hidden="true" />}
        </div>
      ))}
    </>
  );
}

/**
 * The shared Models + Effort menu sections rendered inside both harness pickers
 * — the in-session composer (ChatPage) and the landing dialog (NewChatDialog).
 *
 * Each page supplies the option data, labels, callbacks, and any search/loading
 * slot; the section structure (header, separator, checkbox rows) lives here so
 * the two surfaces render the same composed menu instead of drifting into
 * separate page-local copies. Pass a section as undefined to omit it.
 */
export function ComposerConfigSections({
  sdk,
  routing,
  models,
  efforts,
  extra,
}: {
  sdk?: ComposerConfigSection;
  // Optional Smart Routing section rendered above Models. It is a routing
  // choice, not a model: giving it its own labeled section keeps the panel
  // titled by its content instead of reading as "the Smart Routing list".
  routing?: ComposerConfigSection;
  models?: ComposerConfigSection;
  efforts?: ComposerConfigSection;
  // Additional sections rendered after Models/Effort — e.g. Devin Fusion's
  // Lead / Effort / Sidekick selectors. Each gets its own separator + header.
  extra?: readonly ComposerConfigSection[];
}) {
  const sections = [
    ...(sdk ? [sdk] : []),
    ...(routing ? [routing] : []),
    ...(models ? [models] : []),
    ...(efforts ? [efforts] : []),
    ...(extra ?? []),
  ];
  return (
    <>
      {sections.map((section, index) => (
        <div key={section.testId} data-testid={section.testId}>
          {index > 0 && <DropdownMenuSeparator />}
          <PickerSectionHeader>{section.header}</PickerSectionHeader>
          {section.leading}
          {section.groups ? (
            <ConfigGroupedChoices groups={section.groups} />
          ) : (
            <ConfigChoices choices={section.choices} />
          )}
        </div>
      ))}
    </>
  );
}
