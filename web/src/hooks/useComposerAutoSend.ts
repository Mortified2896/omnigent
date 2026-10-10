import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/** A prepared draft sends once after five seconds, unless the user takes over. */
export function useComposerAutoSend(onSend: () => void) {
  const latest = useRef(onSend);
  useLayoutEffect(() => {
    latest.current = onSend;
  });
  const deadline = useRef<number | null>(null);
  const interval = useRef<ReturnType<typeof setInterval> | null>(null);
  const [seconds, setSeconds] = useState<number | null>(null);
  const stop = useCallback(() => {
    if (interval.current !== null) clearInterval(interval.current);
    interval.current = null;
    deadline.current = null;
    setSeconds(null);
  }, []);
  const start = useCallback(() => {
    stop();
    deadline.current = Date.now() + 5000;
    setSeconds(5);
    interval.current = setInterval(() => {
      const remaining = Math.ceil(((deadline.current ?? Date.now()) - Date.now()) / 1000);
      if (remaining <= 0) {
        stop();
        latest.current();
      } else setSeconds(remaining);
    }, 100);
  }, [stop]);
  useEffect(() => {
    const hide = () => {
      if (document.hidden) stop();
    };
    document.addEventListener("visibilitychange", hide);
    return () => {
      document.removeEventListener("visibilitychange", hide);
      stop();
    };
  }, [stop]);
  return { seconds, start, stop };
}
