import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { useLiveProfiles, type SchemaSource } from "~/components/studio/live-connections";

type ChatDraft = { prompt: string; source: SchemaSource | null };
const emptyDraft: ChatDraft = { prompt: "", source: null };

type WorkspaceSessionValue = {
  drafts: Record<string, ChatDraft>;
  connections: ReturnType<typeof useLiveProfiles>;
  activeChatPath: string;
  setActiveChatPath: (path: string) => void;
  updateDraft: (key: string, patch: Partial<ChatDraft>) => void;
  moveDraft: (from: string, to: string) => void;
  clearDraft: (key: string) => void;
  clearSession: () => void;
};

const WorkspaceSessionContext = createContext<WorkspaceSessionValue | null>(null);

export function WorkspaceSessionProvider({ children }: { children: ReactNode }) {
  const [drafts, setDrafts] = useState<Record<string, ChatDraft>>({});
  const [activeChatPath, setActiveChatPath] = useState("/app");
  const connections = useLiveProfiles();

  const updateDraft = useCallback((key: string, patch: Partial<ChatDraft>) => {
    setDrafts((current) => ({ ...current, [key]: { ...emptyDraft, ...current[key], ...patch } }));
  }, []);
  const moveDraft = useCallback((from: string, to: string) => {
    setDrafts((current) => {
      const next = { ...current, [to]: current[from] ?? emptyDraft };
      delete next[from];
      return next;
    });
  }, []);
  const clearDraft = useCallback((key: string) => {
    setDrafts((current) => { const next = { ...current }; delete next[key]; return next; });
  }, []);
  const clearSession = useCallback(() => {
    setDrafts({});
    setActiveChatPath("/app");
    connections.setProfiles([]);
    connections.setActive("");
  }, [connections.setActive, connections.setProfiles]);

  const value = useMemo(() => ({ drafts, connections, activeChatPath, setActiveChatPath, updateDraft, moveDraft, clearDraft, clearSession }),
    [drafts, connections, activeChatPath, updateDraft, moveDraft, clearDraft, clearSession]);
  return <WorkspaceSessionContext.Provider value={value}>{children}</WorkspaceSessionContext.Provider>;
}

export function useWorkspaceSession() {
  const session = useContext(WorkspaceSessionContext);
  if (!session) throw new Error("WorkspaceSessionProvider is required.");
  return session;
}
