/** Controlled provider-group settings section registered in the live composer.
 * Parent owns authenticated v2 API loading/saving and a scope-stable idPrefix.
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
  error?: string | null;
  onChange: (preferences: ProviderPreferences) => void;
  onSave: () => void;
}

export function ProviderSettingsPanel(props: ProviderSettingsPanelProps) {
  const { value, options, busy = false, idPrefix, onChange } = props;
  if (!value) return <p role="status">Loading saved advisor settings…</p>;
  const knownIds = new Set(options.map((option) => option.choice_id));
  return (
    <section className="advisor-providers" aria-label="Model advisor settings">
      <header>
        <h3>Model advisor</h3>
        <p>Choose models and reasoning levels. Connections are handled separately.</p>
      </header>
      <fieldset disabled={busy}>
        <label className="advisor-switch">
          <input
            type="checkbox"
            role="switch"
            checked={value.enabled}
            onChange={(event) => onChange({ ...value, enabled: event.currentTarget.checked })}
          />
          Compare my choice with the advisor
        </label>
        <p>Allowed answers — shared by you and the advisor</p>
        {PROVIDER_GROUPS.map((provider) => (
          <ProviderCard
            key={provider}
            provider={provider}
            value={value}
            options={options}
            idPrefix={idPrefix}
            onChange={onChange}
          />
        ))}
        <fieldset className="advisor-own-model">
          <legend>Advisor model and reasoning</legend>
          <label htmlFor={`${idPrefix}-advisor`}>Choose the advisor independently</label>
          <select
            id={`${idPrefix}-advisor`}
            value={value.advisor_choice_id ?? ""}
            onChange={(event) =>
              onChange({ ...value, advisor_choice_id: event.currentTarget.value || null })
            }
          >
            <option value="">Choose advisor model + reasoning…</option>
            {value.advisor_choice_id && !knownIds.has(value.advisor_choice_id) ? (
              <option value={value.advisor_choice_id} disabled>
                Saved advisor unavailable — choose explicitly
              </option>
            ) : null}
            {PROVIDER_GROUPS.map((provider) => (
              <optgroup key={provider} label={PROVIDER_LABELS[provider]}>
                {groupModels(options, provider).flatMap((model) =>
                  model.options.map((option) => (
                    <option
                      key={option.choice_id}
                      value={option.choice_id}
                      disabled={!option.available}
                    >
                      {model.display_name} · {effortLabel(option.reasoning_effort)}
                      {!option.available ? " — unavailable" : ""}
                    </option>
                  )),
                )}
              </optgroup>
            ))}
          </select>
          <p>Turning off a provider above pauses its answer choices, not this advisor.</p>
        </fieldset>
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
          <span role="status">{activeChoiceIds(value).length} active combinations</span>
          <button type="button" disabled={!props.dirty} onClick={props.onSave}>
            Save defaults
          </button>
          <p>
            {props.dirty
              ? "Unsaved changes apply only to this round until saved."
              : "Defaults are saved for new chats."}
          </p>
        </footer>
      </fieldset>
      {props.error ? <p role="alert">{props.error}</p> : null}
    </section>
  );
}

interface ProviderCardProps {
  provider: ProviderGroup;
  value: ProviderPreferences;
  options: readonly LogicalOption[];
  idPrefix: string;
  onChange: (preferences: ProviderPreferences) => void;
}

function ProviderCard({ provider, value, options, idPrefix, onChange }: ProviderCardProps) {
  const selected = value.providers[provider];
  const models = groupModels(options, provider);
  const visibleIds = new Set(
    models.flatMap((model) => model.options.map((option) => option.choice_id)),
  );
  const missing = selected.selected_choice_ids.filter((id) => !visibleIds.has(id));
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
          {selected.selected_choice_ids.length} combinations remembered
          {selected.enabled ? "" : " · paused"}
        </p>
      </div>
      <div id={panelId} hidden={selected.collapsed}>
        <fieldset className="advisor-transport">
          <legend>Connection preference</legend>
          <label>
            <input
              type="radio"
              name={`${idPrefix}-${provider}-transport`}
              checked={selected.transport_preference === "omniroute_preferred"}
              onChange={() => onChange(selectTransport(value, provider, "omniroute_preferred"))}
            />
            OmniRoute preferred · Direct fallback
          </label>
          <label>
            <input
              type="radio"
              name={`${idPrefix}-${provider}-transport`}
              checked={selected.transport_preference === "direct_only"}
              onChange={() => onChange(selectTransport(value, provider, "direct_only"))}
            />
            Direct only
          </label>
          <p>
            The advisor chooses the model, not the connection. Fallback keeps the same model,
            reasoning and plan.
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
            <legend>{model.display_name}</legend>
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
