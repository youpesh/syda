import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { HugeiconsIcon } from "@hugeicons/react";
import { Add01Icon, AiMagicIcon, Analytics01Icon, Logout01Icon, Settings02Icon } from "@hugeicons/core-free-icons";
import { MessageSquareText } from "lucide-react";

import { Button, buttonVariants } from "~/components/ui/button";
import { Separator } from "~/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "~/components/ui/sidebar";
import type { SavedConversationSummary } from "~/lib/studio-types";

export function AppShell({
  title,
  subtitle,
  children,
  headerActions,
  onNewScenario,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  headerActions?: ReactNode;
  onNewScenario?: () => void;
}) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [recentConversations, setRecentConversations] = useState<SavedConversationSummary[]>([]);
  const [authenticated, setAuthenticated] = useState(false);
  const [displayName, setDisplayName] = useState("Account");

  useEffect(() => {
    let active = true;
    fetch("/api/auth/me").then(async (response) => {
      if (response.ok) {
        const account = await response.json();
        if (active) {
          setAuthenticated(true);
          setDisplayName(account.display_name || account.email || "Account");
        }
      } else {
        navigate(`/login?next=${encodeURIComponent(pathname)}`, { replace: true });
      }
    }).catch(() => { if (active) navigate(`/login?next=${encodeURIComponent(pathname)}`, { replace: true }); });
    return () => { active = false; };
  }, [navigate, pathname]);

  useEffect(() => {
    let active = true;
    const refresh = () => fetch("/api/conversations?limit=30")
      .then((response) => response.ok ? response.json() : [])
      .then((items) => { if (active) setRecentConversations(items); })
      .catch(() => {});
    void refresh();
    window.addEventListener("syda:conversations-changed", refresh);
    return () => { active = false; window.removeEventListener("syda:conversations-changed", refresh); };
  }, [pathname]);

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader className="gap-3 p-3">
          <Link className="flex h-8 items-center gap-2 px-1" to="/app" aria-label="Syda workspace">
            <span className="flex size-6 items-center justify-center bg-primary text-primary-foreground"><HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} /></span>
            <span className="text-sm font-semibold tracking-[-0.02em] group-data-[collapsible=icon]:hidden">Syda</span>
          </Link>
          {onNewScenario ? (
            <Button className="w-full group-data-[collapsible=icon]:px-0" onClick={onNewScenario} variant="outline">
              <HugeiconsIcon data-icon="inline-start" icon={Add01Icon} strokeWidth={2} /><span className="group-data-[collapsible=icon]:hidden">New scenario</span>
            </Button>
          ) : (
            <Link className={buttonVariants({ variant: "outline", className: "w-full group-data-[collapsible=icon]:px-0" })} to="/app">
              <HugeiconsIcon data-icon="inline-start" icon={Add01Icon} strokeWidth={2} /><span className="group-data-[collapsible=icon]:hidden">New scenario</span>
            </Link>
          )}
        </SidebarHeader>

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Recent chats</SidebarGroupLabel>
            <SidebarGroupContent>
              {recentConversations.length > 0 ? (
                <SidebarMenu>
                  {recentConversations.map((conversation) => <SidebarMenuItem key={conversation.id}>
                    <SidebarMenuButton isActive={pathname === `/app/${conversation.id}`} render={<Link to={`/app/${conversation.id}`} />} tooltip={conversation.title}>
                      <MessageSquareText /><span>{conversation.title}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>)}
                </SidebarMenu>
              ) : (
                <p className="px-2 py-1 text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">Your chats will appear here.</p>
              )}
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter className="border-t border-sidebar-border p-1">
          <SidebarGroup className="p-1">
            <SidebarGroupLabel>Workspace</SidebarGroupLabel>
            <SidebarGroupContent><SidebarMenu>
              <SidebarMenuItem><SidebarMenuButton isActive={pathname === "/scenarios"} render={<Link to="/scenarios" />} tooltip="Scenarios"><HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} /><span>Scenarios</span></SidebarMenuButton></SidebarMenuItem>
              <SidebarMenuItem><SidebarMenuButton isActive={pathname.startsWith("/history") || pathname.startsWith("/runs/")} render={<Link to="/history" />} tooltip="Generation history"><HugeiconsIcon icon={Analytics01Icon} strokeWidth={2} /><span>Generation history</span></SidebarMenuButton></SidebarMenuItem>
              <SidebarMenuItem><SidebarMenuButton isActive={pathname === "/settings"} render={<Link to="/settings" />} tooltip="Settings"><HugeiconsIcon icon={Settings02Icon} strokeWidth={2} /><span>Settings</span></SidebarMenuButton></SidebarMenuItem>
              <SidebarMenuItem><SidebarMenuButton onClick={async () => { await fetch("/api/auth/cookie/logout", { method: "POST" }); navigate("/", { replace: true }); }} tooltip={`Log out ${displayName}`}><HugeiconsIcon icon={Logout01Icon} strokeWidth={2} /><span>Log out</span></SidebarMenuButton></SidebarMenuItem>
            </SidebarMenu></SidebarGroupContent>
          </SidebarGroup>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <SidebarInset className="h-svh overflow-hidden">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b px-4">
          <div className="flex min-w-0 items-center gap-2">
            <SidebarTrigger />
            <Separator className="h-4" orientation="vertical" />
            <div className="min-w-0"><p className="truncate text-xs font-medium">{title}</p>{subtitle && <p className="hidden truncate text-[10px] text-muted-foreground sm:block">{subtitle}</p>}</div>
          </div>
          {headerActions}
        </header>
        {authenticated ? children : <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">Checking your session…</div>}
      </SidebarInset>
    </SidebarProvider>
  );
}
