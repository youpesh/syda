import React from "react";

interface CleanMessageTextProps {
  text: string;
  isStreaming?: boolean;
  isUser?: boolean;
  hasScenario?: boolean;
}

/**
 * Strips raw markdown syntax tokens (###, **, *, `, etc.) so assistant responses
 * render as crisp, clean plain text without visual clutter.
 */
export function cleanMarkdown(raw: string): string {
  if (!raw) return "";
  let text = raw;
  // Replace markdown header lines (e.g. "### Key Scenario Configurations:") with clean headers
  text = text.replace(/^#{1,6}\s*(.+?):?\s*$/gm, "$1:");
  text = text.replace(/\s*#{1,6}\s+/g, "\n\n");
  // Remove bold and italics: **text**, *text*, __text__, _text_
  text = text.replace(/\*\*([^*]+)\*\*/g, "$1");
  text = text.replace(/\*([^*]+)\*/g, "$1");
  text = text.replace(/__([^_]+)__/g, "$1");
  text = text.replace(/_([^_]+)_/g, "$1");
  // Remove code backticks: `code` -> code
  text = text.replace(/`([^`]+)`/g, "$1");
  // Clean markdown bullet prefixes (* item or - item) to clean bullets
  text = text.replace(/^(\s*)[-*]\s+/gm, "$1• ");
  // Strip blockquote markers
  text = text.replace(/^>\s*/gm, "");
  // Normalize excess consecutive newlines
  text = text.replace(/\n{3,}/g, "\n\n");
  return text.trim();
}

/**
 * CleanMessageText renders chat messages in an elegant, human-friendly format:
 * - Markdown syntax tokens are cleanly stripped
 * - Paragraphs and structured lists have generous, breathable line spacing
 * - Streaming pulse animation is placed seamlessly at the end
 */
export function CleanMessageText({
  text,
  isStreaming = false,
  isUser = false,
  hasScenario = false,
}: CleanMessageTextProps) {
  if (!text) return null;

  const baseText = isUser ? text.split("\n\nDatabase schema context (")[0] : cleanMarkdown(text);
  const hasSchemaContext = isUser && text.includes("\n\nDatabase schema context (");

  if (isUser) {
    return (
      <div className="text-sm leading-relaxed whitespace-pre-wrap break-words">
        {baseText}
        {hasSchemaContext && (
          <span className="mt-2 block text-xs text-muted-foreground/90 font-normal">
            Database schema included with this prompt.
          </span>
        )}
      </div>
    );
  }

  // Split into paragraphs by double newlines
  const paragraphs = baseText.split(/\n\n+/).filter((p) => p.trim().length > 0);

  return (
    <div className={`space-y-2.5 text-sm leading-relaxed text-foreground/90 ${hasScenario ? "mb-3.5" : ""}`}>
      {paragraphs.map((paragraph, pIdx) => {
        const isLastParagraph = pIdx === paragraphs.length - 1;
        const lines = paragraph.split("\n").filter((l) => l.trim().length > 0);

        return (
          <div key={pIdx} className="space-y-1">
            {lines.map((line, lIdx) => {
              const trimmed = line.trim();
              const isBullet = trimmed.startsWith("• ");
              const isNumbered = /^\d+\.\s/.test(trimmed);
              const isItem = isBullet || isNumbered;
              const isLastLine = isLastParagraph && lIdx === lines.length - 1;

              return (
                <p
                  key={lIdx}
                  className={`break-words ${
                    isItem ? "pl-2.5 text-foreground/85 py-0.5" : ""
                  }`}
                >
                  {trimmed}
                  {isStreaming && isLastLine && (
                    <span className="ml-1.5 inline-block h-3.5 w-1.5 animate-pulse rounded-xs bg-primary align-middle" />
                  )}
                </p>
              );
            })}
          </div>
        );
      })}
      {isStreaming && paragraphs.length === 0 && (
        <span className="inline-block h-3.5 w-1.5 animate-pulse rounded-xs bg-primary align-middle" />
      )}
    </div>
  );
}
