import { ActionIcon, Group, Text } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconLayoutSidebar } from "@tabler/icons-react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import type { SearchHit } from "../api/types";
import { useWorkspace } from "../graph/WorkspaceProvider";
import { useTenantSession } from "../session/SessionProvider";
import { visibleViews } from "../views/registry";
import classes from "./AppLayout.module.css";
import { NodeSearch, type PickMode } from "./NodeSearch";
import { Sidebar } from "./Sidebar";
import { AccountMenu, ActivityBell, TenantSwitcher } from "./ToolbarMenus";

/**
 * The window: one toolbar (tenant, view title, activity, search, account),
 * the sidebar menu, and the current view. A search hit opens in Explore.
 */
export function AppLayout() {
  const session = useTenantSession();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const location = useLocation();
  const [sidebarOpen, sidebar] = useDisclosure(true);
  const views = visibleViews(session);
  const current = views.find((view) => location.pathname.startsWith(view.path));

  const openHit = (hit: SearchHit, mode: PickMode) => {
    const ref = { id: String(hit.properties.id ?? hit.id), label: hit.label };
    if (mode === "add") workspace.model.addNode(ref, hit.properties, workspace.selected ?? undefined);
    else workspace.open(ref, hit.properties);
    if (!location.pathname.startsWith("/explore")) navigate("/explore");
  };

  return (
    <div className={classes.root} data-sidebar={sidebarOpen ? "open" : "closed"}>
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
          <NodeSearch hotkey allowAdd onPick={openHit} width={220} />
          <AccountMenu />
        </Group>
      </header>
      {sidebarOpen ? (
        <aside className={classes.sidebar}>
          <Sidebar views={views} />
        </aside>
      ) : null}
      <main className={classes.main}>
        <Outlet />
      </main>
    </div>
  );
}
