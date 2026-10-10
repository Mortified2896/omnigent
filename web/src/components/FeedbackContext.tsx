import { createContext, type ReactNode, type RefObject } from "react";
import type { ExperimentEvent, TaskOutcome } from "@/hooks/useTaskExperiment";
import type { Session } from "@/lib/types";

export const FeedbackFormContext = createContext<{
  sessionId: string;
  hostId: string | null;
  human: Map<string, ExperimentEvent>;
  ready: boolean;
  renderPerspective?: (responseId: string, review: ReviewPerspectiveInput) => ReactNode;
} | null>(null);

export function FeedbackDisabled({ children }: { children: ReactNode }) {
  return <FeedbackFormContext.Provider value={null}>{children}</FeedbackFormContext.Provider>;
}

export interface ReviewPerspectiveInput {
  outcome: TaskOutcome;
  comment: string;
  tags: string[];
  saved: boolean;
  onAddTag: (tag: string) => void;
  onAppendComment: (text: string) => void;
  onReplaceDetails: (details: { comment: string; tags: string[] }) => void | Promise<void>;
}
export interface Thread {
  session_id: string;
  response_id: string;
  inherited_ids: string[];
  original_feedback?: Original | null;
}
export interface Original {
  outcome: string;
  comment: string;
  tags: string[];
  model?: string;
  reasoning_effort?: string;
}
export interface Active {
  responseId: string;
  branchId: string;
}
export const FeedbackDiscussionContext = createContext<{
  sessionId: string;
  threads: Thread[];
  active: Active | null;
  setActive: (active: Active | null) => void;
  wide: boolean;
  currentChatSendNonce: number;
  onCurrentChatSend: () => void;
  reviews: RefObject<Map<string, ReviewPerspectiveInput>>;
  renderTranscript: (active: Active) => ReactNode;
} | null>(null);

export function readOriginal(session: Session | undefined, thread?: Thread): Original | null {
  if (thread?.original_feedback) return thread.original_feedback;
  try {
    const raw = session?.labels?.["omnigent.feedback.original"];
    return raw ? (JSON.parse(raw) as Original) : null;
  } catch {
    return null;
  }
}
