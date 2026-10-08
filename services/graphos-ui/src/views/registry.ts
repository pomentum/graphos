import {
  IconActivity,
  IconChartBar,
  IconKey,
  IconRoute,
  IconSchema,
  IconTable,
  IconTopologyStar3,
  type Icon,
} from "@tabler/icons-react";
import type { ParseKeys } from "i18next";
import type { Session } from "../session/SessionProvider";

export type ViewGroup = "Graph" | "Insights" | "Data" | "Admin";

/** One screen of the app: where it lives in the menu and who may open it. */
export interface ViewDefinition {
  id: string;
  path: string;
  /** The i18n key of the view's name, translated where it is shown. */
  title: ParseKeys;
  icon: Icon;
  group: ViewGroup;
  /** `read`: anyone on the tenant; `owner`: its owners and admins; `admin`: admins only. */
  requires: "read" | "owner" | "admin";
}

/** The i18n key of each menu group's heading. */
export const GROUP_TITLES: Record<ViewGroup, ParseKeys> = {
  Graph: "views.registry.groups.graph",
  Insights: "views.registry.groups.insights",
  Data: "views.registry.groups.data",
  Admin: "views.registry.groups.admin",
};

/** The screens, in menu order. Add a view here and it appears in the sidebar and the router. */
export const VIEWS: ViewDefinition[] = [
  {
    id: "explore",
    path: "/explore",
    title: "views.registry.titles.explore",
    icon: IconTopologyStar3,
    group: "Graph",
    requires: "read",
  },
  { id: "path", path: "/path", title: "views.registry.titles.path", icon: IconRoute, group: "Graph", requires: "read" },
  {
    id: "overview",
    path: "/overview",
    title: "views.registry.titles.overview",
    icon: IconChartBar,
    group: "Insights",
    requires: "read",
  },
  {
    id: "activity",
    path: "/activity",
    title: "views.registry.titles.activity",
    icon: IconActivity,
    group: "Insights",
    requires: "read",
  },
  {
    id: "browse",
    path: "/browse",
    title: "views.registry.titles.browse",
    icon: IconTable,
    group: "Data",
    requires: "read",
  },
  {
    id: "schema",
    path: "/schema",
    title: "views.registry.titles.schema",
    icon: IconSchema,
    group: "Data",
    requires: "read",
  },
  {
    id: "access",
    path: "/access",
    title: "views.registry.titles.access",
    icon: IconKey,
    group: "Admin",
    requires: "owner",
  },
];

/** The views this session may open; views it can't use are hidden, not greyed out. */
export function visibleViews(
  session: Pick<Session, "isAdmin" | "isOwner">,
  views: ViewDefinition[] = VIEWS,
): ViewDefinition[] {
  return views.filter(
    (view) => view.requires === "read" || session.isAdmin || (view.requires === "owner" && session.isOwner),
  );
}

/** Visible views grouped for the sidebar, groups in menu order. */
export function menuGroups(views: ViewDefinition[]): [ViewGroup, ViewDefinition[]][] {
  const groups = new Map<ViewGroup, ViewDefinition[]>();
  for (const view of views) groups.set(view.group, [...(groups.get(view.group) ?? []), view]);
  return [...groups.entries()];
}
