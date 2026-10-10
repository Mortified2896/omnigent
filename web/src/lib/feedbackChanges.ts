export interface FeedbackTextChange {
  kind: "unchanged" | "removed" | "added";
  text: string;
}

export function feedbackOutcomeLabel(value: string): string {
  return (
    { success: "Success", partial: "Partial", failed: "Failed", not_sure: "Not sure" }[value] ??
    value
  );
}

function words(text: string): string[] {
  if (typeof Intl.Segmenter === "function") {
    return Array.from(
      new Intl.Segmenter(undefined, { granularity: "word" }).segment(text),
      (part) => part.segment,
    );
  }
  return text.match(/\s+|[\p{L}\p{N}]+|[^\s\p{L}\p{N}]/gu) ?? [];
}

/** Preserve both versions exactly while marking edits at word boundaries. */
export function diffFeedbackText(before: string, after: string): FeedbackTextChange[] {
  if (before === after) return before ? [{ kind: "unchanged", text: before }] : [];
  const oldWords = words(before);
  const newWords = words(after);
  let start = 0;
  while (start < oldWords.length && start < newWords.length && oldWords[start] === newWords[start])
    start++;
  let oldEnd = oldWords.length;
  let newEnd = newWords.length;
  while (oldEnd > start && newEnd > start && oldWords[oldEnd - 1] === newWords[newEnd - 1]) {
    oldEnd--;
    newEnd--;
  }
  const oldMiddle = oldWords.slice(start, oldEnd);
  const newMiddle = newWords.slice(start, newEnd);
  const changes: FeedbackTextChange[] = [];
  const append = (kind: FeedbackTextChange["kind"], text: string) => {
    if (!text) return;
    const last = changes.at(-1);
    if (last?.kind === kind) last.text += text;
    else changes.push({ kind, text });
  };
  append("unchanged", oldWords.slice(0, start).join(""));
  // Bound work on a phone. Unrelated long comments become one replacement;
  // prefix and suffix text still remain unchanged and both versions are exact.
  if (oldMiddle.length * newMiddle.length > 250_000) {
    append("removed", oldMiddle.join(""));
    append("added", newMiddle.join(""));
  } else {
    const stride = newMiddle.length + 1;
    const scores = new Uint16Array((oldMiddle.length + 1) * stride);
    for (let i = oldMiddle.length - 1; i >= 0; i--) {
      for (let j = newMiddle.length - 1; j >= 0; j--) {
        scores[i * stride + j] =
          oldMiddle[i] === newMiddle[j]
            ? scores[(i + 1) * stride + j + 1] + 1
            : Math.max(scores[(i + 1) * stride + j], scores[i * stride + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < oldMiddle.length || j < newMiddle.length) {
      if (i < oldMiddle.length && j < newMiddle.length && oldMiddle[i] === newMiddle[j]) {
        append("unchanged", oldMiddle[i++]);
        j++;
      } else if (
        i < oldMiddle.length &&
        (j === newMiddle.length || scores[(i + 1) * stride + j] >= scores[i * stride + j + 1])
      ) {
        append("removed", oldMiddle[i++]);
      } else {
        append("added", newMiddle[j++]);
      }
    }
  }
  append("unchanged", oldWords.slice(oldEnd).join(""));
  return changes;
}
