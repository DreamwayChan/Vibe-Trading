import { useId, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Dotted term with a hover / keyboard card.
 *
 * Do not also set HTML ``title`` — the browser tooltip would repeat the same
 * copy a moment later.
 */
export function TermHint({
  hint,
  children,
  className,
}: {
  hint: string;
  children: ReactNode;
  className?: string;
}) {
  const tooltipId = useId();
  if (!hint) {
    return <>{children}</>;
  }
  return (
    <span
      className={cn("relative inline-flex group/term cursor-help", className)}
      aria-describedby={tooltipId}
    >
      <span className="border-b border-dotted border-muted-foreground/70">{children}</span>
      <span
        id={tooltipId}
        role="tooltip"
        className="pointer-events-none absolute left-1/2 top-full z-50 mt-1 hidden w-64 -translate-x-1/2 rounded-md border bg-background px-3 py-2 text-left text-xs leading-relaxed text-foreground group-hover/term:block"
      >
        {hint}
      </span>
    </span>
  );
}
