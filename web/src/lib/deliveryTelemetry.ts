export type DeliveryPhase = "receive" | "apply" | "disconnect" | "reconnect" | "reconcile";

export interface DeliveryTelemetryEntry {
  phase: DeliveryPhase;
  timestamp: number;
  connectionId: string | null;
  conversationId: string;
  responseId: string | null;
  eventSequence: number | null;
  eventType: string | null;
  snapshotWatermark: string | null;
  clientAppliedWatermark: string | null;
  classification: string | null;
}

const MAX_ENTRIES = 200;
let entries: DeliveryTelemetryEntry[] = [];

/** Record allowlisted delivery metadata. Message/tool payloads are not accepted. */
export function recordDeliveryTelemetry(entry: DeliveryTelemetryEntry): void {
  entries = [...entries.slice(-(MAX_ENTRIES - 1)), entry];
}

export function snapshotDeliveryTelemetry(): readonly DeliveryTelemetryEntry[] {
  return entries;
}

export function clearDeliveryTelemetry(): void {
  entries = [];
}
