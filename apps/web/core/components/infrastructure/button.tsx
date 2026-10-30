import type { ComponentProps } from "react";
import { Button as NativeButton } from "@plane/propel/button";

/** Keep the native control with usable touch targets on narrow project pages. */
export function Button({ className = "", ...props }: ComponentProps<typeof NativeButton>) {
  return <NativeButton {...props} className={`min-h-11 min-w-11 sm:min-h-0 sm:min-w-0 ${className}`} />;
}
