import { attachmentFilename } from "./attachments";

export interface AdvisorAttachment {
  filename: string;
  content_type: string;
  data: string;
}

const encodings = new WeakMap<File, Promise<AdvisorAttachment>>();
const identities = new WeakMap<File, number>();
let nextIdentity = 0;

export function advisorAttachmentIdentity(files: readonly File[]): number[] {
  return files.map((file) => {
    let identity = identities.get(file);
    if (identity === undefined) {
      identity = ++nextIdentity;
      identities.set(file, identity);
    }
    return identity;
  });
}

export async function encodeAdvisorAttachments(
  files: readonly File[],
): Promise<AdvisorAttachment[]> {
  if (files.length > 20 || files.reduce((total, file) => total + file.size, 0) > 50 * 1024 * 1024) {
    throw new Error("Advisor accepts up to 20 attachments totaling 50 MB per message.");
  }
  const attachments: AdvisorAttachment[] = [];
  // Read sequentially to keep large file reads from multiplying memory use.
  for (const file of files) {
    let encoding = encodings.get(file);
    if (!encoding) {
      encoding = new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error(`Couldn't read ${attachmentFilename(file)}.`));
        reader.onload = () => {
          const result = reader.result;
          if (typeof result !== "string" || !result.includes(",")) {
            reject(new Error(`Couldn't read ${attachmentFilename(file)}.`));
            return;
          }
          resolve({
            filename: attachmentFilename(file),
            content_type: file.type,
            data: result.slice(result.indexOf(",") + 1),
          });
        };
        reader.readAsDataURL(file);
      });
      encodings.set(file, encoding);
      void encoding.catch(() => encodings.delete(file));
    }
    // Sequential reads bound peak memory for large uploads.
    // eslint-disable-next-line no-await-in-loop
    attachments.push(await encoding);
  }
  return attachments;
}
