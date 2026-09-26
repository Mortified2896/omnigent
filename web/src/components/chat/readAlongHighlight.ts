export interface ReadAlongUnit {
  text: string;
  narration_start: number;
  narration_end: number;
  start_seconds: number;
  end_seconds: number;
}

export interface VisibleReadAlongRange {
  unitIndex: number;
  range: Range;
}

interface VisibleWord {
  key: string;
  start: number;
  end: number;
}

interface TextPosition {
  node: Text;
  offset: number;
}

interface FlattenedText {
  text: string;
  positions: (TextPosition | null)[];
}

const BLOCK_TAGS = new Set([
  "ARTICLE",
  "BLOCKQUOTE",
  "DIV",
  "H1",
  "H2",
  "H3",
  "H4",
  "H5",
  "H6",
  "LI",
  "OL",
  "P",
  "SECTION",
  "TABLE",
  "TBODY",
  "TD",
  "TH",
  "THEAD",
  "TR",
  "UL",
]);
const WORD_RE = /[\p{L}\p{M}\p{N}]+/gu;
const MAX_COMPOUND_PARTS = 8;
const MAX_ALIGNMENT_CELLS = 16_000_000;

function wordKey(value: string): string {
  return value
    .normalize("NFKC")
    .toLocaleLowerCase("en-US")
    .replace(/[^\p{L}\p{M}\p{N}]/gu, "");
}

function isExcludedTextNode(node: Text, root: Element): boolean {
  let parent = node.parentElement;
  while (parent && parent !== root) {
    if (
      parent.tagName === "PRE" ||
      parent.tagName === "SCRIPT" ||
      parent.tagName === "STYLE" ||
      parent.getAttribute("aria-hidden") === "true"
    ) {
      return true;
    }
    parent = parent.parentElement;
  }
  return false;
}

function nearestBlock(node: Text, root: Element): Element {
  let parent = node.parentElement;
  while (parent && parent !== root) {
    if (BLOCK_TAGS.has(parent.tagName)) return parent;
    parent = parent.parentElement;
  }
  return root;
}

function flattenVisibleText(root: Element): FlattenedText {
  const document = root.ownerDocument;
  const showText = document.defaultView?.NodeFilter.SHOW_TEXT ?? 4;
  const walker = document.createTreeWalker(root, showText);
  const chunks: string[] = [];
  const positions: (TextPosition | null)[] = [];
  let previousBlock: Element | null = null;

  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType !== 3) continue;
    const textNode = node as Text;
    if (isExcludedTextNode(textNode, root)) continue;
    const value = textNode.nodeValue ?? "";
    if (!value) continue;

    const block = nearestBlock(textNode, root);
    const lastCharacter = chunks.at(-1)?.at(-1) ?? "";
    if (
      previousBlock &&
      block !== previousBlock &&
      lastCharacter &&
      !/\s/u.test(lastCharacter) &&
      !/^\s/u.test(value)
    ) {
      chunks.push(" ");
      positions.push(null);
    }
    chunks.push(value);
    for (let offset = 0; offset < value.length; offset += 1) {
      positions.push({ node: textNode, offset });
    }
    previousBlock = block;
  }
  return { text: chunks.join(""), positions };
}

function visibleWords(text: string): VisibleWord[] {
  const words: VisibleWord[] = [];
  for (const match of text.matchAll(WORD_RE)) {
    const value = match[0];
    const start = match.index ?? 0;
    words.push({ key: wordKey(value), start, end: start + value.length });
  }
  return words;
}

function matchWordAt(
  words: VisibleWord[],
  text: string,
  startIndex: number,
  target: string,
): { first: number; last: number } | null {
  let combined = "";
  for (
    let index = startIndex;
    index < words.length && index < startIndex + MAX_COMPOUND_PARTS;
    index += 1
  ) {
    if (index > startIndex) {
      const between = text.slice(words[index - 1]!.end, words[index]!.start);
      if (/\s/u.test(between)) break;
    }
    combined += words[index]!.key;
    if (combined === target) return { first: startIndex, last: index };
    if (!target.startsWith(combined)) break;
  }
  return null;
}

function createRange(
  document: Document,
  positions: (TextPosition | null)[],
  start: number,
  end: number,
): Range | null {
  const first = positions[start];
  const last = positions[end - 1];
  if (!first || !last) return null;
  const range = document.createRange();
  range.setStart(first.node, first.offset);
  range.setEnd(last.node, last.offset + 1);
  return range;
}

/**
 * Monotonically map spoken timing units onto rendered assistant text.
 * Unmatched narration words are skipped; unmatched visible text never shifts
 * later timing units onto the wrong word.
 */
export function mapReadAlongRanges(
  sections: Iterable<Element>,
  units: ReadAlongUnit[],
): VisibleReadAlongRange[] {
  const sectionList = Array.from(sections);
  const combinedText: string[] = [];
  const combinedPositions: (TextPosition | null)[] = [];
  let priorLastCharacter = "";
  for (const section of sectionList) {
    const flattened = flattenVisibleText(section);
    if (!flattened.text) continue;
    if (priorLastCharacter && !/\s/u.test(priorLastCharacter) && !/^\s/u.test(flattened.text)) {
      combinedText.push(" ");
      combinedPositions.push(null);
    }
    combinedText.push(flattened.text);
    for (const position of flattened.positions) combinedPositions.push(position);
    priorLastCharacter = flattened.text.at(-1) ?? priorLastCharacter;
  }

  const text = combinedText.join("");
  const words = visibleWords(text);
  const mapped: VisibleReadAlongRange[] = [];
  const document = sectionList[0]?.ownerDocument;
  if (!document || words.length === 0) return mapped;

  // Align the entire sequence rather than greedily matching an early word.
  // Narrated work notes may be collapsed while the final answer is visible;
  // their repeated words must not consume matches belonging to the answer.
  const width = words.length + 1;
  const cells = (units.length + 1) * width;
  // Fail open to ordinary audio for unusually large responses, bounding memory.
  if (cells > MAX_ALIGNMENT_CELLS) return mapped;
  const scores = new Uint16Array(cells);
  const keys = units.map((unit) => wordKey(unit.text));
  const spanAt = (unitIndex: number, visibleIndex: number) =>
    keys[unitIndex]!.startsWith(words[visibleIndex]!.key)
      ? matchWordAt(words, text, visibleIndex, keys[unitIndex]!)
      : null;
  for (let i = units.length - 1; i >= 0; i -= 1) {
    for (let j = words.length - 1; j >= 0; j -= 1) {
      const span = spanAt(i, j);
      scores[i * width + j] = Math.max(
        scores[(i + 1) * width + j]!,
        scores[i * width + j + 1]!,
        span ? 1 + scores[(i + 1) * width + span.last + 1]! : 0,
      );
    }
  }
  let i = 0;
  let j = 0;
  while (i < units.length && j < words.length) {
    const score = scores[i * width + j]!;
    // Prefer skipping narration on a tie: hidden preambles should not steal
    // identical words from the visible final answer that follows them.
    if (score === scores[(i + 1) * width + j]) {
      i += 1;
      continue;
    }
    const span = spanAt(i, j);
    if (span && score === 1 + scores[(i + 1) * width + span.last + 1]!) {
      const range = createRange(
        document,
        combinedPositions,
        words[j]!.start,
        words[span.last]!.end,
      );
      if (range) mapped.push({ unitIndex: i, range });
      i += 1;
      j = span.last + 1;
    } else {
      j += 1;
    }
  }
  return mapped;
}

/** Binary search the sorted timing units; gaps and out-of-range times return -1. */
export function activeReadAlongUnitIndex(units: ReadAlongUnit[], seconds: number): number {
  if (!Number.isFinite(seconds) || units.length === 0) return -1;
  let low = 0;
  let high = units.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (units[middle]!.start_seconds <= seconds) low = middle + 1;
    else high = middle;
  }
  const index = low - 1;
  if (index < 0) return -1;
  const unit = units[index]!;
  return seconds < unit.end_seconds ? index : -1;
}

interface HighlightLike {
  add: (range: Range) => void;
  clear: () => void;
}

interface HighlightRegistryLike {
  set: (name: string, highlight: HighlightLike) => void;
  delete: (name: string) => boolean;
}

const HIGHLIGHT_NAME = "omnigent-audio-current-word";
let activeOwner: string | null = null;
let activeHighlight: HighlightLike | null = null;

function highlightApi(): {
  registry: HighlightRegistryLike;
  create: new () => HighlightLike;
} | null {
  if (typeof globalThis.CSS === "undefined") return null;
  const css = globalThis.CSS as typeof CSS & { highlights?: HighlightRegistryLike };
  const constructor = (globalThis as typeof globalThis & { Highlight?: new () => HighlightLike })
    .Highlight;
  if (!css.highlights || !constructor) return null;
  return { registry: css.highlights, create: constructor };
}

/** Apply one range in the browser's global registry, scoped to a player owner. */
export function setReadAlongHighlight(owner: string, range: Range | null): boolean {
  const api = highlightApi();
  if (!api) return false;
  try {
    if (!range) {
      if (activeOwner === owner) {
        activeHighlight?.clear();
        api.registry.delete(HIGHLIGHT_NAME);
        activeOwner = null;
        activeHighlight = null;
      }
      return true;
    }
    if (!activeHighlight) {
      const HighlightConstructor = api.create;
      activeHighlight = new HighlightConstructor();
    }
    activeHighlight.clear();
    activeHighlight.add(range);
    api.registry.set(HIGHLIGHT_NAME, activeHighlight);
    activeOwner = owner;
    return true;
  } catch {
    // Some embedded WebViews expose CSS.highlights without a working registry.
    return false;
  }
}

/** A stale player's cleanup must not remove a newer response's highlight. */
export function clearReadAlongHighlight(owner: string): void {
  if (activeOwner !== owner) return;
  const api = highlightApi();
  try {
    activeHighlight?.clear();
    api?.registry.delete(HIGHLIGHT_NAME);
  } catch {
    // The text and native audio controls remain usable without highlighting.
  }
  activeOwner = null;
  activeHighlight = null;
}
