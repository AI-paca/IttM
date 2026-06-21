import { Check, ClipboardPaste } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { MouseEvent, RefObject } from "react";

interface PasteButtonProps {
  targetRef?: RefObject<HTMLInputElement | null>;
  onPaste: (text: string) => void;
  title?: string;
}

type PasteState = "idle" | "manual" | "done";

export function PasteButton({
  targetRef,
  onPaste,
  title = "Вставить",
}: PasteButtonProps) {
  const [state, setState] = useState<PasteState>("idle");
  const cleanupRef = useRef<(() => void) | null>(null);
  const resetTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      cleanupRef.current?.();
      if (resetTimerRef.current !== null) {
        window.clearTimeout(resetTimerRef.current);
      }
    };
  }, []);

  const resetSoon = () => {
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current);
    }
    resetTimerRef.current = window.setTimeout(() => setState("idle"), 1200);
  };

  const commitPaste = (text: string) => {
    onPaste(text.trim());
    setState("done");
    resetSoon();
  };

  const armManualPaste = () => {
    cleanupRef.current?.();
    targetRef?.current?.focus();
    targetRef?.current?.select();
    setState("manual");

    const handlePaste = (event: ClipboardEvent) => {
      const text = event.clipboardData?.getData("text");
      if (!text) return;
      event.preventDefault();
      commitPaste(text);
      cleanupRef.current?.();
      cleanupRef.current = null;
    };

    window.addEventListener("paste", handlePaste, { capture: true });
    const timeout = window.setTimeout(() => {
      cleanupRef.current?.();
      cleanupRef.current = null;
      setState("idle");
    }, 8000);

    cleanupRef.current = () => {
      window.removeEventListener("paste", handlePaste, { capture: true });
      window.clearTimeout(timeout);
    };
  };

  const handleClick = async (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    cleanupRef.current?.();
    cleanupRef.current = null;

    try {
      const text = await navigator.clipboard?.readText();
      if (text) {
        commitPaste(text);
        return;
      }
    } catch {
      // Fallback below: focus the field and consume the next user paste.
    }

    armManualPaste();
  };

  const isManual = state === "manual";
  const isDone = state === "done";
  const Icon = isDone ? Check : ClipboardPaste;
  const label = isManual ? "Нажмите Ctrl+V" : title;

  return (
    <button
      type="button"
      onClick={handleClick}
      className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border shadow-sm outline-none transition-all active:scale-95 focus-visible:ring-2 focus-visible:ring-[var(--color-accent-ring)] ${
        isManual || isDone
          ? "border-[var(--color-accent-soft-border)] bg-[var(--color-accent-soft)] text-[var(--color-accent-strong)]"
          : "border-[var(--color-border-default)] bg-[var(--color-bg-inset)] text-[var(--color-text-muted)] hover:border-[var(--color-accent-soft-border)] hover:text-[var(--color-accent)]"
      }`}
      title={label}
      aria-label={label}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}
