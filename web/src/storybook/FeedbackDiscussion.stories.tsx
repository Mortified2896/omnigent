import type { Meta, StoryObj } from "@storybook/react-vite";
import { FeedbackDiscussionPrototype } from "./FeedbackDiscussionPrototype";

const meta = {
  title: "Prototypes/Feedback discussion",
  component: FeedbackDiscussionPrototype,
  args: {
    variant: "footer",
    startingPoint: "before-feedback",
    responseDelayMs: 650,
    cacheTelemetry: "unreported",
  },
  argTypes: {
    variant: { control: false },
    startingPoint: {
      control: "select",
      options: [
        "before-feedback",
        "feedback-saved",
        "discussion-ready",
        "inspection-ready",
        "suggestion-ready",
      ],
      description: "Start at a different step of the same conversation.",
    },
    responseDelayMs: {
      control: { type: "range", min: 0, max: 2000, step: 100 },
      description: "Delay for simulated AI replies; no model requests.",
    },
    cacheTelemetry: {
      control: "radio",
      options: ["reported", "unreported"],
      description: "Simulated provider counters versus unavailable usage; prototype only.",
    },
  },
  parameters: { layout: "fullscreen", omnigentSurface: "chat" },
  render: (args) => (
    <FeedbackDiscussionPrototype
      key={`${args.variant}:${args.startingPoint}:${args.responseDelayMs}:${args.cacheTelemetry}`}
      {...args}
    />
  ),
} satisfies Meta<typeof FeedbackDiscussionPrototype>;
export default meta;
type Story = StoryObj<typeof meta>;

export const FooterAction: Story = { name: "Footer action", args: { variant: "footer" } };
export const QuickPrompts: Story = { name: "Quick prompts", args: { variant: "quick-prompts" } };
export const InlineProposal: Story = { name: "Inline discussion", args: { variant: "inline" } };

export const PromptTags: Story = {
  name: "Advisor prompt tags",
  args: { variant: "footer", startingPoint: "suggestion-ready", showPromptTagSuggestions: true },
};
