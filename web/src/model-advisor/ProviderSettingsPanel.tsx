/** Controlled provider-group settings section registered in the live composer.
 * Parent owns authenticated preference loading/saving and a scope-stable idPrefix.
 * Deliberately no logos, no "manage models" gate and no transport in model choice.
 */
import type { LogicalOption, ProviderPreferences, ProviderGroup } from "./providerPreferences";
import {
  PROVIDER_GROUPS,
  PROVIDER_LABELS,
  activeChoiceIds,
  effortLabel,
  groupModels,
  selectTransport,
  toggleEffort,
  toggleModel,
  toggleProvider,
  updateProvider,
} from "./providerPreferences";
import "./provider-settings.css";

export interface ProviderSettingsPanelProps {
  idPrefix: string;
  value: ProviderPreferences | null;
  options: readonly LogicalOption[];
  dirty: boolean;
  busy?: boolean;
  enabledLocked?: boolean;
  error?: string | null;
  onChange: (preferences: ProviderPreferences) => void;
  onSave: () => void;
  /** Optional external control for Storybook; production persists per-effort approval. */
  approvalChoices?: { ids: readonly string[]; onChange: (ids: string[]) => void };
  omniRouteOnly?: {
    providers: readonly ProviderGroup[];
    onChange: (provider: ProviderGroup, only: boolean) => void;
  };
}

export function ProviderSettingsPanel(props: ProviderSettingsPanelProps) {
  const { value, options, busy = false, idPrefix, onChange } = props;
  if (!value) return <p role="status">Loading saved advisor settings…</p>;
  const switchControl = (
    <label className="advisor-switch">
      <input
        type="checkbox"
        role="switch"
        aria-label="Compare my choice with the advisor"
        checked={value.enabled}
        disabled={busy || props.enabledLocked}
        onChange={(event) => onChange({ ...value, enabled: event.currentTarget.checked })}
      />
      <span>{value.enabled ? "ON" : "OFF"}</span>
    </label>
  );
  if (!value.enabled) {
    return (
      <section className="advisor-providers" aria-label="Model advisor settings">
        <div className="advisor-off-row">
          <div className="advisor-off-copy">
            <h3>Model advisor</h3>
            <p>Compare your model choice with an advisor.</p>
          </div>
          {switchControl}
        </div>
        {props.error ? <p role="alert">{props.error}</p> : null}
      </section>
    );
  }
  return (
    <section className="advisor-providers" aria-label="Model advisor settings">
      <header>
        <div className="advisor-on-heading">
          <div>
            <h3>Model advisor</h3>
            <p>Choose models and reasoning levels. Connections are handled separately.</p>
          </div>
          {switchControl}
        </div>
      </header>
      <fieldset disabled={busy}>
        <p>Allowed answers — shared by you and the advisor</p>
        {PROVIDER_GROUPS.map((provider) => (
          <ProviderCard
            key={provider}
            provider={provider}
            value={value}
            options={options}
            idPrefix={idPrefix}
            onChange={onChange}
            approvalChoices={props.approvalChoices}
            omniRouteOnly={props.omniRouteOnly}
          />
        ))}
        <label htmlFor={`${idPrefix}-balance`}>
          Decision balance: {value.human_probability_percent}% me /{" "}
          {100 - value.human_probability_percent}% advisor
        </label>
        <input
          id={`${idPrefix}-balance`}
          className="advisor-balance"
          type="range"
          min={0}
          max={100}
          step={5}
          value={value.human_probability_percent}
          onChange={(event) =>
            onChange({ ...value, human_probability_percent: Number(event.currentTarget.value) })
          }
        />
        {value.unresolved_legacy_ids.length ? (
          <fieldset className="advisor-unavailable">
            <legend>Unresolved saved choices</legend>
            <p>These old choices could not be mapped safely. Nothing was silently replaced.</p>
            {value.unresolved_legacy_ids.map((id) => (
              <div key={id} className="advisor-missing">
                <span>{id}</span>
                <button
                  type="button"
                  onClick={() =>
                    onChange({
                      ...value,
                      unresolved_legacy_ids: value.unresolved_legacy_ids.filter(
                        (key) => key !== id,
                      ),
                    })
                  }
                >
                  Remove saved reference
                </button>
              </div>
            ))}
          </fieldset>
        ) : null}
        <footer>
          <span role="status">{activeChoiceIds(value, options).length} active combinations</span>
          <button type="button" disabled={!props.dirty} onClick={props.onSave}>
            Save defaults
          </button>
          <p>
            {props.dirty
              ? "Changes apply to this chat. Save defaults to use them for new chats."
              : "Defaults are saved for new chats."}
          </p>
        </footer>
      </fieldset>
      {props.error ? <p role="alert">{props.error}</p> : null}
    </section>
  );
}

interface ProviderCardProps {
  approvalChoices?: ProviderSettingsPanelProps["approvalChoices"];
  omniRouteOnly?: ProviderSettingsPanelProps["omniRouteOnly"];
  provider: ProviderGroup;
  value: ProviderPreferences;
  options: readonly LogicalOption[];
  idPrefix: string;
  onChange: (preferences: ProviderPreferences) => void;
}

function ProviderCard({
  provider,
  value,
  options,
  idPrefix,
  onChange,
  approvalChoices,
  omniRouteOnly,
}: ProviderCardProps) {
  const selected = value.providers[provider];
  const onlyOmniRoute =
    omniRouteOnly?.providers.includes(provider) ??
    selected.transport_preference === "omniroute_only";
  const guardedChoices = approvalChoices ?? {
    ids: [
      ...(selected.approval_choice_ids ?? []),
      ...options
        .filter((option) => (selected.approval_model_ids ?? []).includes(option.model_id))
        .map((option) => option.choice_id),
    ],
    onChange: (ids: string[]) =>
      onChange(
        updateProvider(value, provider, { approval_choice_ids: ids, approval_model_ids: [] }),
      ),
  };
  const models = groupModels(options, provider);
  const qualified = models.flatMap((model) => model.options).filter((option) => option.available);
  const canUseOmniRoute = qualified.some((option) => option.access_lanes.includes("omniroute"));
  const canUseDirect = qualified.some((option) =>
    option.access_lanes.some((lane) => lane.endsWith("-direct")),
  );
  const visibleIds = new Set(
    models.flatMap((model) => model.options.map((option) => option.choice_id)),
  );
  const missing = selected.selected_choice_ids.filter((id) => !visibleIds.has(id));
  const rememberedCombinationCount = selected.selected_choice_ids.length;
  const panelId = `${idPrefix}-${provider}-models`;
  const titleId = `${idPrefix}-${provider}-title`;
  return (
    <section className="advisor-provider-card" aria-labelledby={titleId}>
      <div className="advisor-provider-heading">
        <button
          id={titleId}
          className="advisor-disclosure"
          type="button"
          aria-expanded={!selected.collapsed}
          aria-controls={panelId}
          onClick={() =>
            onChange(updateProvider(value, provider, { collapsed: !selected.collapsed }))
          }
        >
          <span aria-hidden="true">{selected.collapsed ? "▸" : "▾"}</span>
          <span>{PROVIDER_LABELS[provider]}</span>
        </button>
        <label className="advisor-switch" title="Pause answers without clearing selections">
          <input
            type="checkbox"
            role="switch"
            checked={selected.enabled}
            aria-label={`Enable ${provider === "openai" ? "OpenAI" : "GLM"} answers`}
            onChange={(event) =>
              onChange(toggleProvider(value, provider, event.currentTarget.checked))
            }
          />
          <span>{selected.enabled ? "On" : "Off"}</span>
        </label>
        <p className="advisor-provider-summary">
          {rememberedCombinationCount}{" "}
          {rememberedCombinationCount === 1 ? "combination" : "combinations"} remembered
          {selected.enabled ? "" : " · paused"}
        </p>
      </div>
      <div id={panelId} hidden={selected.collapsed}>
        <fieldset className="advisor-transport">
          <legend>Connection preference</legend>
          {models.length > 0 ? (
            <label>
              <input
                type="radio"
                name={`${idPrefix}-${provider}-transport`}
                disabled={!canUseOmniRoute}
                checked={
                  canUseOmniRoute &&
                  !onlyOmniRoute &&
                  selected.transport_preference === "omniroute_preferred"
                }
                onChange={() => {
                  omniRouteOnly?.onChange(provider, false);
                  onChange(selectTransport(value, provider, "omniroute_preferred"));
                }}
              />
              OmniRoute preferred · Direct fallback
            </label>
          ) : null}
          {models.length > 0 ? (
            <label>
              <input
                type="radio"
                name={`${idPrefix}-${provider}-transport`}
                disabled={!canUseOmniRoute}
                checked={onlyOmniRoute}
                onChange={() => {
                  omniRouteOnly?.onChange(provider, true);
                  onChange(selectTransport(value, provider, "omniroute_only"));
                }}
              />
              OmniRoute only
            </label>
          ) : null}
          {models.length > 0 ? (
            <label>
              <input
                type="radio"
                name={`${idPrefix}-${provider}-transport`}
                disabled={!canUseDirect}
                checked={
                  canUseDirect &&
                  !onlyOmniRoute &&
                  (selected.transport_preference === "direct_only" || !canUseOmniRoute)
                }
                onChange={() => {
                  omniRouteOnly?.onChange(provider, false);
                  onChange(selectTransport(value, provider, "direct_only"));
                }}
              />
              Direct only
            </label>
          ) : null}
          {models.length > 0 && !canUseOmniRoute ? (
            <p>OmniRoute is not currently qualified for this provider on this host.</p>
          ) : null}
          {models.length > 0 && !canUseDirect ? (
            <p>Direct is not currently qualified for this provider on this host.</p>
          ) : null}
          {!canUseOmniRoute && !canUseDirect ? (
            <p role="status">No qualified connection is currently available.</p>
          ) : null}
          <p>
            {onlyOmniRoute
              ? "Use OmniRoute without direct fallback."
              : "The advisor chooses the model, not the connection. Fallback keeps the same model, reasoning and plan."}
          </p>
          {value.route_review_required.includes(provider) ? (
            <div>
              <p role="alert">Confirm a connection preference before using migrated choices.</p>
              <button
                type="button"
                onClick={() =>
                  onChange(selectTransport(value, provider, selected.transport_preference))
                }
              >
                Confirm connection preference
              </button>
            </div>
          ) : null}
        </fieldset>
        {!selected.enabled ? (
          <p className="advisor-paused">Answers paused. Your checked levels stay saved.</p>
        ) : null}
        {models.length === 0 ? (
          <p>No qualified models in this provider's current catalog.</p>
        ) : null}
        {models.map((model) => (
          <fieldset key={model.model_id} className="advisor-model-row">
            <legend>
              <span>{model.display_name}</span>
              <label className="advisor-model-switch">
                <span className="sr-only">Enable {model.display_name} answers</span>
                <input
                  type="checkbox"
                  role="switch"
                  checked={!selected.disabled_model_ids.includes(model.model_id)}
                  aria-label={`Enable ${model.display_name} answers`}
                  onChange={(event) =>
                    onChange(
                      toggleModel(value, provider, model.model_id, event.currentTarget.checked),
                    )
                  }
                />
                <span>{selected.disabled_model_ids.includes(model.model_id) ? "Off" : "On"}</span>
              </label>
            </legend>
            {guardedChoices ? (
              <div className="mb-3">
                <p>Ask for approval by reasoning level</p>
                <div className="advisor-efforts">
                  {model.options.map((option) => (
                    <label
                      key={option.choice_id}
                      className={`advisor-effort advisor-approval-effort${guardedChoices.ids.includes(option.choice_id) ? " is-selected" : ""}`}
                    >
                      <input
                        type="checkbox"
                        aria-label={`Ask before running ${model.display_name} at ${effortLabel(option.reasoning_effort)}`}
                        checked={guardedChoices.ids.includes(option.choice_id)}
                        onChange={(event) => {
                          const ids = new Set(guardedChoices.ids);
                          if (event.currentTarget.checked) ids.add(option.choice_id);
                          else ids.delete(option.choice_id);
                          guardedChoices.onChange([...ids]);
                        }}
                      />
                      {effortLabel(option.reasoning_effort)}
                    </label>
                  ))}
                </div>
              </div>
            ) : null}
            <div className="advisor-efforts">
              {model.options.map((option) => {
                const checked = selected.selected_choice_ids.includes(option.choice_id);
                return (
                  <label
                    key={option.choice_id}
                    className={`advisor-effort${checked ? " is-selected" : ""}`}
                    title={
                      !option.available ? (option.unavailable_reason ?? "Unavailable") : undefined
                    }
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!option.available && !checked}
                      aria-label={`${model.display_name}: ${effortLabel(option.reasoning_effort)}`}
                      onChange={(event) =>
                        onChange(
                          toggleEffort(
                            value,
                            provider,
                            option.choice_id,
                            event.currentTarget.checked,
                          ),
                        )
                      }
                    />
                    {effortLabel(option.reasoning_effort)}
                    {!option.available ? (
                      <span className="advisor-unavailable-label">unavailable</span>
                    ) : null}
                  </label>
                );
              })}
            </div>
          </fieldset>
        ))}
        {missing.map((id) => (
          <label key={id} className="advisor-missing">
            <input
              type="checkbox"
              checked
              onChange={() => onChange(toggleEffort(value, provider, id, false))}
            />
            <span>Unavailable saved choice: {id}. Uncheck to remove.</span>
          </label>
        ))}
      </div>
    </section>
  );
}
