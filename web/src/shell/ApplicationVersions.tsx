import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";

/** The official server version and the fork revision, shared across shell layouts. */
export function ApplicationVersions({
  versions,
}: {
  versions: { official: string; private: string };
}) {
  return (
    <button
      type="button"
      title="Copy official and private versions"
      onClick={async () => {
        try {
          await copyText(
            `Omnigent\nOfficial base v${versions.official}\nPrivate ${versions.private}`,
          );
          showToast("Versions copied");
        } catch {
          showToast("Couldn’t copy versions. Please try again.");
        }
      }}
      className="flex cursor-pointer flex-col rounded-sm text-left text-[10px] leading-3 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
      aria-label="Copy application versions"
    >
      <span>Official base v{versions.official}</span>
      <span title="Private customization Git revision">Private {versions.private}</span>
    </button>
  );
}
