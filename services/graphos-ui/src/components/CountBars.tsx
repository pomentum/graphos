import { Text, UnstyledButton } from "@mantine/core";
import { useTranslation } from "react-i18next";
import { formatCount } from "../lib/time";
import classes from "./CountBars.module.css";

export interface CountRow {
  name: string;
  count: number;
  /** Extra text after the count, e.g. a size. */
  detail?: string;
}

export interface CountBarsProps {
  rows: CountRow[];
  /** Makes rows clickable. */
  onPick?: (name: string) => void;
  /** The highlighted row. */
  active?: string | null;
  /** Shown when there are no rows; "Nothing yet" by default. */
  empty?: string;
}

/** Names with counts and bars scaled to the largest, as grouped rows. */
export function CountBars({ rows, onPick, active, empty }: CountBarsProps) {
  const { t } = useTranslation();
  const max = Math.max(1, ...rows.map((r) => r.count));
  if (rows.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        {empty ?? t("components.countBars.empty")}
      </Text>
    );
  }
  return (
    <div className={classes.group}>
      {rows.map((row) => {
        const content = (
          <>
            <div className={classes.line}>
              <Text size="sm" truncate title={row.name}>
                {row.name || t("components.countBars.emptyName")}
              </Text>
              <Text size="xs" c="dimmed" className={classes.count}>
                {formatCount(row.count)}
                {row.detail ? ` · ${row.detail}` : ""}
              </Text>
            </div>
            <div className={classes.track}>
              <div className={classes.bar} style={{ width: `${(row.count / max) * 100}%` }} />
            </div>
          </>
        );
        return onPick ? (
          <UnstyledButton
            key={row.name}
            className={classes.row}
            data-active={active === row.name}
            onClick={() => onPick(row.name)}
          >
            {content}
          </UnstyledButton>
        ) : (
          <div key={row.name} className={classes.row}>
            {content}
          </div>
        );
      })}
    </div>
  );
}
