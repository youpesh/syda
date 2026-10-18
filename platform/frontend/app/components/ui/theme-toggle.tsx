import type * as React from "react";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "~/lib/theme-provider";
import { Button } from "~/components/ui/button";
import { cn } from "cn";

interface ThemeToggleProps extends Omit<React.ComponentProps<typeof Button>, "onClick"> {
  showLabel?: boolean;
}

export function ThemeToggle({
  className,
  variant = "ghost",
  size = "icon",
  showLabel = false,
  ...props
}: ThemeToggleProps) {
  const { resolvedTheme, toggleTheme } = useTheme();
  const isDark = resolvedTheme === "dark";

  return (
    <Button
      type="button"
      variant={variant}
      size={showLabel ? "default" : size}
      onClick={toggleTheme}
      className={cn(
        "relative select-none transition-colors",
        !showLabel && "size-9",
        className
      )}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      {...props}
    >
      <div className="relative flex items-center justify-center">
        <Sun
          className={cn(
            "size-4 transition-all duration-300",
            isDark ? "rotate-0 scale-100 opacity-100" : "-rotate-90 scale-0 opacity-0 absolute"
          )}
        />
        <Moon
          className={cn(
            "size-4 transition-all duration-300",
            isDark ? "rotate-90 scale-0 opacity-0 absolute" : "rotate-0 scale-100 opacity-100"
          )}
        />
      </div>
      {showLabel && (
        <span className="ml-2 text-xs">
          {isDark ? "Light mode" : "Dark mode"}
        </span>
      )}
      <span className="sr-only">Toggle theme</span>
    </Button>
  );
}
