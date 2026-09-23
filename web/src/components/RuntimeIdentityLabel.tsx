import { useServerInfo } from "@/lib/CapabilitiesContext";
import { cn } from "@/lib/utils";

function shortBuildSha(sha: string | null | undefined): string | null {
  return typeof sha === "string" && /^[a-f0-9]{40}$/.test(sha) ? `git-${sha.slice(0, 12)}` : null;
}

/** Compact identity for the running release, not the browser's checkout. */
export function RuntimeIdentityLabel({ className }: { className?: string }) {
  const info = useServerInfo();
  if (info === "loading") return null;
  const instance = info.instance_id === "O1" || info.instance_id === "O2" ? info.instance_id : null;
  const custom = shortBuildSha(info.build_sha);
  if (instance === null && custom === null && info.server_version === null) return null;
  const label = [instance ?? "instance unknown", custom ?? "build unknown", info.server_version]
    .filter(Boolean)
    .join(" · ");
  const title = [
    instance ? `Instance ${instance}` : "Instance identity not recorded",
    info.build_sha ? `Full build SHA: ${info.build_sha}` : "Build SHA not recorded",
    info.server_version ? `Installed package: ${info.server_version}` : "Package version unknown",
  ].join("\n");
  return (
    <div
      className={cn("truncate px-2 text-[11px] text-muted-foreground", className)}
      data-testid="runtime-identity-label"
      title={title}
    >
      {label}
    </div>
  );
}
