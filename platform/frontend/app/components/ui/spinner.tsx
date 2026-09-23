import { Loading03Icon } from "@hugeicons/core-free-icons";
import { HugeiconsIcon } from "@hugeicons/react";

import { cn } from "~/lib/utils";

function Spinner({
  className,
  ...props
}: Omit<React.ComponentProps<typeof HugeiconsIcon>, "icon" | "strokeWidth">) {
  return (
    <HugeiconsIcon
      aria-label="Loading"
      className={cn("size-4 animate-spin", className)}
      data-slot="spinner"
      icon={Loading03Icon}
      role="status"
      strokeWidth={2}
      {...props}
    />
  );
}

export { Spinner };
