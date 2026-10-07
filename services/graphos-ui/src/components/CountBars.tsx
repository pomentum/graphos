import { Text, UnstyledButton } from "@mantine/core";
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
  empty?: string;
}

/** Names with counts and bars scaled to the largest, as grouped rows. */
export function CountBars({ rows, onPick, active, empty = "Nothing yet" }: CountBarsProps) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  if (rows.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        {empty}
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
                {row.name || "(empty)"}
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
