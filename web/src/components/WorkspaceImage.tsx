import { useEffect, useState } from "react";
import { fileContentToBlob, useFileContent } from "@/hooks/useFileContent";
import { resolveChatFilePath } from "@/hooks/useWorkspaceChangedFiles";
import { useFileViewerConversationId, useWorkspacePaths } from "@/shell/FileViewerContext";
import { workspaceImagePath } from "@/lib/workspaceImages";
import { ZoomableImage } from "./ImageLightbox";

/** A local image belongs to the session host, never the viewer's filesystem. */
export function WorkspaceImage({ path, alt }: { path: string; alt: string }) {
  const conversationId = useFileViewerConversationId();
  const { root, home } = useWorkspacePaths();
  const safePath = workspaceImagePath(path);
  const resolved = safePath ? resolveChatFilePath(safePath, root, home) : null;
  if (!conversationId || !resolved)
    return (
      <span role="img" aria-label={alt}>
        Image unavailable: {alt}
      </span>
    );
  return <ResolvedWorkspaceImage conversationId={conversationId} path={resolved.path} alt={alt} />;
}

function ResolvedWorkspaceImage({
  conversationId,
  path,
  alt,
}: {
  conversationId: string;
  path: string;
  alt: string;
}) {
  const { data, isError } = useFileContent(conversationId, path);
  const [loaded, setLoaded] = useState<{ data: typeof data; url: string } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setLoaded(null);
    setFailed(false);
    if (!data) return;
    if (
      data.truncated ||
      data.bytes > 10 * 1024 * 1024 ||
      !/^image\/(?:png|jpeg|gif|webp|avif|bmp)$/i.test(data.content_type ?? "")
    ) {
      setFailed(true);
      return;
    }
    let url: string;
    try {
      url = URL.createObjectURL(fileContentToBlob(data));
    } catch {
      setFailed(true);
      return;
    }
    setLoaded({ data, url });
    return () => URL.revokeObjectURL(url);
  }, [data]);
  if (isError || failed)
    return (
      <span role="img" aria-label={alt}>
        Image unavailable: {alt}
      </span>
    );
  if (!loaded || loaded.data !== data)
    return (
      <span role="status" aria-label={`Loading image: ${alt}`}>
        Loading image…
      </span>
    );
  return (
    <span className="inline-flex max-w-full items-start" data-testid="workspace-image">
      <ZoomableImage
        src={loaded.url}
        alt={alt}
        className="max-h-96 max-w-full"
        onError={() => setFailed(true)}
      />
    </span>
  );
}
