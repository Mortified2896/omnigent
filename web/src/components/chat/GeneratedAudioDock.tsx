import {
  createContext,
  useContext,
  useMemo,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";

interface AudioDock {
  element: HTMLDivElement | null;
  activeKey: string | null;
  select: Dispatch<SetStateAction<string | null>>;
  bind: Dispatch<SetStateAction<HTMLDivElement | null>>;
}

export const GeneratedAudioDockContext = createContext<AudioDock | null>(null);

/** Keep playback controls outside the scrolling transcript, above the composer. */
export function GeneratedAudioDockProvider({ children }: { children: ReactNode }) {
  const [element, bind] = useState<HTMLDivElement | null>(null);
  const [activeKey, select] = useState<string | null>(null);
  const value = useMemo(() => ({ element, activeKey, select, bind }), [element, activeKey]);
  return (
    <GeneratedAudioDockContext.Provider value={value}>
      {children}
    </GeneratedAudioDockContext.Provider>
  );
}

export function GeneratedAudioDock() {
  const dock = useContext(GeneratedAudioDockContext);
  return (
    <div
      ref={dock?.bind}
      data-testid="generated-audio-dock"
      className="mx-auto w-full max-w-3xl shrink-0 px-4 pb-2 empty:hidden min-[2561px]:max-w-[clamp(56rem,30vw,64rem)]"
    />
  );
}
