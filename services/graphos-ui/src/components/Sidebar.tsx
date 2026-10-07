import { Text } from "@mantine/core";
import { NavLink } from "react-router-dom";
import { menuGroups, type ViewDefinition } from "../views/registry";
import classes from "./Sidebar.module.css";

/** The menu of views, grouped like a macOS source list; the current view is highlighted. */
export function Sidebar({ views }: { views: ViewDefinition[] }) {
  return (
    <nav className={classes.root} aria-label="Views">
      {menuGroups(views).map(([group, items]) => (
        <div key={group} className={classes.section}>
          <Text size="xs" c="dimmed" className={classes.heading}>
            {group}
          </Text>
          {items.map((view) => (
            <NavLink
              key={view.id}
              to={view.path}
              className={({ isActive }) => (isActive ? `${classes.item} ${classes.active}` : classes.item)}
            >
              <view.icon size={16} stroke={1.7} className={classes.icon} />
              <span>{view.title}</span>
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}
