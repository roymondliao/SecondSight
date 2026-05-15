import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

type BadgeVariant = "label" | "category" | "mono";

const BADGE_VARIANTS: Record<BadgeVariant, string> = {
  label: "font-mono text-[11px] uppercase tracking-[0.22em]",
  category: "text-[11px] font-medium",
  mono: "font-mono text-[11px]",
};

type BadgeProps = HTMLAttributes<HTMLSpanElement> & {
  variant?: BadgeVariant;
};

export function Badge({ className, variant = "label", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "chip-pill text-muted-foreground",
        BADGE_VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}
