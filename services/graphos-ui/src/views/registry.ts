import { IconActivity, IconChartBar, IconRoute, IconTable, IconTopologyStar3, type Icon } from "@tabler/icons-react";
import type { Session } from "../session/SessionProvider";

export type ViewGroup = "Graph" | "Insights" | "Data" | "Admin";

/** One screen of the app: where it lives in the menu and who may open it. */
export interface ViewDefinition {
  id: string;
  path: string;
  title: string;
  icon: Icon;
  group: ViewGroup;
  /** `read`: any token on the tenant; `admin`: admin tokens only. */
  requires: "read" | "admin";
}

/** The screens, in menu order. Add a view here and it appears in the sidebar and the router. */
export const VIEWS: ViewDefinition[] = [
  { id: "explore", path: "/explore", title: "Explore", icon: IconTopologyStar3, group: "Graph", requires: "read" },
  { id: "path", path: "/path", title: "Path", icon: IconRoute, group: "Graph", requires: "read" },
  { id: "overview", path: "/overview", title: "Overview", icon: IconChartBar, group: "Insights", requires: "read" },
  { id: "activity", path: "/activity", title: "Activity", icon: IconActivity, group: "Insights", requires: "read" },
  { id: "browse", path: "/browse", title: "Browse", icon: IconTable, group: "Data", requires: "read" },
];

/** The views this session may open; views it can't use are hidden, not greyed out. */
export function visibleViews(session: Pick<Session, "isAdmin">, views: ViewDefinition[] = VIEWS): ViewDefinition[] {
  return views.filter((view) => view.requires === "read" || session.isAdmin);
}

/** Visible views grouped for the sidebar, groups in menu order. */
export function menuGroups(views: ViewDefinition[]): [ViewGroup, ViewDefinition[]][] {
  const groups = new Map<ViewGroup, ViewDefinition[]>();
  for (const view of views) groups.set(view.group, [...(groups.get(view.group) ?? []), view]);
  return [...groups.entries()];
}
