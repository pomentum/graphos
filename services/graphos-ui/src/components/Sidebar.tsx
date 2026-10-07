import { Text } from "@mantine/core";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, useLocation } from "react-router-dom";
import { GROUP_TITLES, menuGroups, type ViewDefinition } from "../views/registry";
import classes from "./Sidebar.module.css";

/**
 * The menu of views, grouped like a macOS source list; the current view is
 * highlighted. Each link returns to where the user last was in that view
 * (its query string: Browse's label and filter, Path's ends, …), so moving
 * between views does not lose a search.
 */
export function Sidebar({ views }: { views: ViewDefinition[] }) {
  const { t } = useTranslation();
  const location = useLocation();
  const lastSearch = useRef(new Map<string, string>());
  useEffect(() => {
    lastSearch.current.set(location.pathname, location.search);
  }, [location.pathname, location.search]);

  return (
    <nav className={classes.root} aria-label={t("shell.sidebar.views")}>
      {menuGroups(views).map(([group, items]) => (
        <div key={group} className={classes.section}>
          <Text size="xs" c="dimmed" className={classes.heading}>
            {t(GROUP_TITLES[group])}
          </Text>
          {items.map((view) => (
            <NavLink
              key={view.id}
              to={`${view.path}${lastSearch.current.get(view.path) ?? ""}`}
              className={({ isActive }) => (isActive ? `${classes.item} ${classes.active}` : classes.item)}
            >
              <view.icon size={16} stroke={1.7} className={classes.icon} />
              <span>{t(view.title)}</span>
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}
