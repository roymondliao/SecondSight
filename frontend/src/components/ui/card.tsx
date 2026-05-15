import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-card border border-white/70 bg-white/55 p-5 shadow-ambient backdrop-blur-xl",
        className,
      )}
      {...props}
    />
  );
}
