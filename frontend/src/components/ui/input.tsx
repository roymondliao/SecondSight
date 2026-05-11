import type { InputHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "h-11 w-full rounded-full border border-border bg-white/80 px-4 text-sm text-foreground shadow-sm outline-none transition-[box-shadow,border-color] duration-150 placeholder:text-muted-foreground focus:border-ring focus:ring-4 focus:ring-ring/15",
        className,
      )}
      {...props}
    />
  );
}
