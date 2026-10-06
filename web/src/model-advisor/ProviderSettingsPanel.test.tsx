import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProviderSettingsPanel } from "./ProviderSettingsPanel";
import {
  emptyProviderPreferences,
  type ProviderGroup,
  type LogicalOption,
} from "./providerPreferences";

const options: LogicalOption[] = ["low", "max"].map((effort) => ({
  choice_id: `gpt-6.1-sol:${effort}`,
  provider: "openai",
  model_id: "gpt-6.1-sol",
  display_name: "GPT-6.1 Sol",
  reasoning_effort: effort,
  model_ids: ["gpt-6.1-sol"],
  access_lanes: ["codex-direct", "omniroute"],
  available: true,
}));

function Settings() {
  const [value, setValue] = useState(() => {
    const defaults = emptyProviderPreferences();
    return {
      ...defaults,
      enabled: true,
      providers: {
        ...defaults.providers,
        openai: {
          ...defaults.providers.openai,
          collapsed: false,
          selected_choice_ids: options.map((option) => option.choice_id),
        },
      },
    };
  });
  const [ids, setIds] = useState<string[]>([]);
  const [onlyProviders, setOnlyProviders] = useState<ProviderGroup[]>([]);
  return (
    <ProviderSettingsPanel
      idPrefix="test"
      value={value}
      options={options}
      dirty={false}
      onChange={setValue}
      onSave={() => {}}
      approvalChoices={{ ids, onChange: setIds }}
      omniRouteOnly={{
        providers: onlyProviders,
        onChange: (provider, only) => setOnlyProviders(only ? [provider] : []),
      }}
    />
  );
}

describe("ProviderSettingsPanel per-choice approval preview", () => {
  it("keeps Low and Max approval independent of each other and answer availability", () => {
    render(<Settings />);
    const low = screen.getByRole("checkbox", { name: "Ask before running GPT-6.1 Sol at Low" });
    const max = screen.getByRole("checkbox", { name: "Ask before running GPT-6.1 Sol at Max" });
    fireEvent.click(max);
    expect(max).toBeChecked();
    expect(low).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "GPT-6.1 Sol: Max" })).toBeChecked();
    fireEvent.click(low);
    fireEvent.click(max);
    expect(low).toBeChecked();
    expect(max).not.toBeChecked();
  });
  it("chooses OmniRoute only without clearing answer choices and can restore fallback", () => {
    render(<Settings />);
    const only = screen.getByRole("radio", { name: "OmniRoute only" });
    const fallback = screen.getByRole("radio", { name: "OmniRoute preferred · Direct fallback" });
    const direct = screen.getByRole("radio", { name: "Direct only" });
    fireEvent.click(only);
    expect(only).toBeChecked();
    expect(fallback).not.toBeChecked();
    expect(direct).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "GPT-6.1 Sol: Max" })).toBeChecked();
    fireEvent.click(fallback);
    expect(fallback).toBeChecked();
    expect(only).not.toBeChecked();
    fireEvent.click(direct);
    expect(direct).toBeChecked();
    expect(fallback).not.toBeChecked();
  });
});
