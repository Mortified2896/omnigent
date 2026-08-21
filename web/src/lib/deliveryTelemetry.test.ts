import { beforeEach, describe, expect, it } from "vitest";
import {
  clearDeliveryTelemetry,
  recordDeliveryTelemetry,
  snapshotDeliveryTelemetry,
} from "./deliveryTelemetry";

describe("delivery telemetry privacy boundary", () => {
  beforeEach(clearDeliveryTelemetry);

  it("stores only allowlisted delivery identifiers and classifications", () => {
    recordDeliveryTelemetry({
      phase: "reconcile",
      timestamp: 1234,
      publishedAt: 1200,
      connectionId: "conn_safe",
      conversationId: "conv_safe",
      responseId: "resp_safe",
      eventSequence: 9,
      eventType: "response.completed",
      snapshotWatermark: "item_9",
      clientAppliedWatermark: "item_9",
      classification: "snapshot_applied",
    });

    const [entry] = snapshotDeliveryTelemetry();
    expect(Object.keys(entry!).sort()).toEqual(
      [
        "phase",
        "timestamp",
        "publishedAt",
        "connectionId",
        "conversationId",
        "responseId",
        "eventSequence",
        "eventType",
        "snapshotWatermark",
        "clientAppliedWatermark",
        "classification",
      ].sort(),
    );
    const serialized = JSON.stringify(entry);
    expect(serialized).not.toMatch(/prompt|assistant_text|tool_arguments|tool_result|credential/i);
  });

  it("keeps a bounded ring across repeated reconnects", () => {
    for (let index = 0; index < 205; index += 1) {
      recordDeliveryTelemetry({
        phase: "reconnect",
        timestamp: index,
        publishedAt: null,
        connectionId: `conn_${index}`,
        conversationId: "conv_safe",
        responseId: null,
        eventSequence: index,
        eventType: null,
        snapshotWatermark: null,
        clientAppliedWatermark: null,
        classification: "transport_drop",
      });
    }
    expect(snapshotDeliveryTelemetry()).toHaveLength(200);
    expect(snapshotDeliveryTelemetry()[0]?.timestamp).toBe(5);
  });
});
