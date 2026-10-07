import { ActionIcon, Group, Text } from "@mantine/core";
import { useDisclosure, useMediaQuery } from "@mantine/hooks";
import { useEffect } from "react";
import { IconLayoutSidebar } from "@tabler/icons-react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import type { SearchHit } from "../api/types";
import { useWorkspace } from "../graph/WorkspaceProvider";
import { useTenantSession } from "../session/SessionProvider";
import { visibleViews } from "../views/registry";
import classes from "./AppLayout.module.css";
import { ErrorBoundary } from "./ErrorBoundary";
import { NodeSearch, type PickMode } from "./NodeSearch";
import { Sidebar } from "./Sidebar";
import { AccountMenu, ActivityBell, TenantSwitcher } from "./ToolbarMenus";

/**
 * The window: one toolbar (tenant, view title, activity, search, account),
 * the sidebar menu, and the current view. A search hit opens in Explore.
 */
/** Windows narrower than this get overlays instead of side-by-side panels. */
const NARROW = "(max-width: 900px)";

export function AppLayout() {
  const session = useTenantSession();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const location = useLocation();
  // Below this width the sidebar lays over the view instead of taking room
  // beside it: closed by default, and closed again after choosing a view.
  const narrow = useMediaQuery(NARROW, false, { getInitialValueInEffect: false });
  const [sidebarOpen, sidebar] = useDisclosure(!narrow);
  useEffect(() => {
    if (narrow) sidebar.close();
    // On moving to another view, or the window becoming narrow.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [narrow, location.pathname]);
  const views = visibleViews(session);
  const current = views.find((view) => location.pathname.startsWith(view.path));

  const openHit = (hit: SearchHit, mode: PickMode) => {
    const ref = { id: String(hit.properties.id ?? hit.id), label: hit.label };
    if (mode === "add") workspace.model.addNode(ref, hit.properties, workspace.selected ?? undefined);
    else workspace.open(ref, hit.properties);
    if (!location.pathname.startsWith("/explore")) navigate("/explore");
  };

  return (
    <div className={classes.root} data-sidebar={sidebarOpen ? "open" : "closed"} data-narrow={narrow || undefined}>
      <header className={classes.toolbar}>
        <Group gap="sm" className={classes.toolbarStart} wrap="nowrap">
          <ActionIcon variant="subtle" color="gray" aria-label="Show or hide the sidebar" onClick={sidebar.toggle}>
            <IconLayoutSidebar size={17} stroke={1.7} />
          </ActionIcon>
          <TenantSwitcher />
        </Group>
        <Text fw={600} size="sm" className={classes.title}>
          {current?.title ?? ""}
        </Text>
        <Group gap="sm" justify="flex-end" wrap="nowrap" className={classes.toolbarEnd}>
          <ActivityBell />
          <NodeSearch hotkey allowAdd onPick={openHit} width={narrow ? 150 : 220} />
          <AccountMenu />
        </Group>
      </header>
      {sidebarOpen ? (
        <aside className={classes.sidebar}>
          <Sidebar views={views} />
        </aside>
      ) : null}
      <main className={classes.main}>
        <ErrorBoundary resetKey={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
