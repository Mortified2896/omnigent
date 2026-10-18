// Adapter from a `RenderItem.reasoning` to the vendored `<Reasoning>`
// chain. Reasoning stays expanded by default; we thread through its
// streaming state, recorded duration, and accumulated text.

import { Reasoning, ReasoningContent, ReasoningTrigger } from "@/components/ai-elements/reasoning";

interface ReasoningViewProps {
  text: string;
  isStreaming: boolean;
  duration: number | undefined;
}

export function ReasoningView({ text, isStreaming, duration }: ReasoningViewProps) {
  // A reasoning section is only worth expanding when it has something to
  // show. While streaming we keep it expandable even before the first
  // chunk lands (the "Thinking..." shimmer is live feedback, and content
  // is on its way). Once settled, an empty section (e.g. a `reasoning_start`
  // with no chunks) renders as a flat header with no dead expand affordance.
  const expandable = isStreaming || text.trim().length > 0;
  return (
    <Reasoning isStreaming={isStreaming} duration={duration} expandable={expandable}>
      <ReasoningTrigger />
      <ReasoningContent>{text}</ReasoningContent>
    </Reasoning>
  );
}
