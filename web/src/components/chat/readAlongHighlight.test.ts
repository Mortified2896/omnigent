import { afterEach, describe, expect, it, vi } from "vitest";
import {
  activeReadAlongUnitIndex,
  clearReadAlongHighlight,
  mapReadAlongRanges,
  setReadAlongHighlight,
  type ReadAlongUnit,
} from "./readAlongHighlight";

afterEach(() => vi.unstubAllGlobals());

function unit(text: string, start: number, end: number): ReadAlongUnit {
  return {
    text,
    narration_start: 0,
    narration_end: text.length,
    start_seconds: start,
    end_seconds: end,
  };
}

describe("read-along timing lookup", () => {
  it("uses binary-search-compatible boundaries and leaves gaps clear", () => {
    const units = [unit("first", 0, 0.4), unit("second", 0.5, 0.9), unit("third", 1.1, 1.5)];
    expect(activeReadAlongUnitIndex(units, 0)).toBe(0);
    expect(activeReadAlongUnitIndex(units, 0.4)).toBe(-1);
    expect(activeReadAlongUnitIndex(units, 0.6)).toBe(1);
    expect(activeReadAlongUnitIndex(units, 1.2)).toBe(2);
    expect(activeReadAlongUnitIndex(units, 1.5)).toBe(-1);
    expect(activeReadAlongUnitIndex(units, Number.NaN)).toBe(-1);
  });

  it("maps monotonic words through Markdown while skipping citations and code blocks", () => {
    const section = document.createElement("div");
    section.innerHTML = `
      <h2>AI priorities</h2>
      <p><strong>OpenAI</strong> and <a href="https://example.test">OpenRouter</a> [1].</p>
      <pre><code>phantom Qwen</code></pre>
      <p>Repeated <em>word</em>, repeated word. Inline <code>GPT-5.5</code>.</p>
      <ul><li>Qwen follows.</li></ul>
    `;
    const spoken = [
      "Next", // clean_narration adds this to some headings; it is not visible.
      "AI",
      "priorities",
      "OpenAI",
      "and",
      "OpenRouter",
      "Repeated",
      "word",
      "repeated",
      "word",
      "GPT-5.5",
      "Qwen",
      "follows",
    ];
    const mapped = mapReadAlongRanges(
      [section],
      spoken.map((text, index) => unit(text, index, index + 0.5)),
    );
    const visible = new Map(mapped.map(({ unitIndex, range }) => [unitIndex, range.toString()]));

    expect(visible.get(0)).toBeUndefined();
    expect(visible.get(1)).toBe("AI");
    expect(visible.get(3)).toBe("OpenAI");
    expect(visible.get(5)).toBe("OpenRouter");
    expect(visible.get(6)).toBe("Repeated");
    expect(visible.get(7)).toBe("word");
    expect(visible.get(8)).toBe("repeated");
    expect(visible.get(9)).toBe("word");
    expect(visible.get(10)).toBe("GPT-5.5");
    expect(visible.get(11)).toBe("Qwen");
    expect(mapped).toHaveLength(spoken.length - 1);
  });

  it("does not advance over visible text when a spoken unit has no visible match", () => {
    const section = document.createElement("div");
    section.innerHTML = "<p>OpenAI <span>unspoken citation words</span> OpenRouter.</p>";
    const spoken = [
      unit("OpenAI", 0, 0.2),
      unit("synthetic", 0.2, 0.3),
      unit("OpenRouter", 0.3, 0.6),
    ];
    const mapped = mapReadAlongRanges([section], spoken);
    expect(mapped.map(({ unitIndex, range }) => [unitIndex, range.toString()])).toEqual([
      [0, "OpenAI"],
      [2, "OpenRouter"],
    ]);
  });

  it("does not let collapsed work narration steal words from the final answer", () => {
    const section = document.createElement("div");
    section.innerHTML =
      "<h2>Daily briefing</h2><p>Current sources and repository evidence deserve attention.</p>";
    const hidden = ["I", "check", "current", "sources", "and", "repository"];
    const visible = [
      "Daily",
      "briefing",
      "Current",
      "sources",
      "and",
      "repository",
      "evidence",
      "deserve",
      "attention",
    ];
    const mapped = mapReadAlongRanges(
      [section],
      [...hidden, ...visible].map((text, i) => unit(text, i, i + 1)),
    );
    expect(mapped.map(({ unitIndex }) => unitIndex)).toEqual(
      visible.map((_, i) => hidden.length + i),
    );
    expect(mapped.map(({ range }) => range.toString())).toEqual(visible);
  });

  it("leaves rendered text untouched when CSS Custom Highlight is unavailable", () => {
    vi.stubGlobal("CSS", undefined);
    vi.stubGlobal("Highlight", undefined);
    const section = document.createElement("p");
    section.textContent = "Audio still works without the read-along API.";
    const range = document.createRange();
    range.selectNodeContents(section.firstChild!);

    expect(setReadAlongHighlight("unsupported", range)).toBe(false);
    expect(section.textContent).toBe("Audio still works without the read-along API.");
    expect(section.childNodes).toHaveLength(1);
    clearReadAlongHighlight("unsupported");
  });
});
