import { Avatar, Badge, Group, Select, Text, UnstyledButton } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { ChangeItem } from "../../api/types";
import { useActivity } from "../../data/ActivityProvider";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { describeChange } from "../../lib/changes";
import { relativeTime } from "../../lib/time";
import classes from "./ActivityView.module.css";

/**
 * Activity: every change to the tenant since this page was opened, live,
 * with who made it. A change to one node opens it in Explore.
 */
export function ActivityView() {
  const { changes, status, markSeen } = useActivity();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [person, setPerson] = useState<string | null>(null);
  const [label, setLabel] = useState<string | null>(null);

  useEffect(markSeen, [markSeen, changes.length]);

  const people = useMemo(() => unique(changes.map((c) => c.consumer ?? "unknown")), [changes]);
  const labels = useMemo(() => unique(changes.map((c) => c.label ?? "")).filter(Boolean), [changes]);
  const shown = changes.filter(
    (c) => (!person || (c.consumer ?? "unknown") === person) && (!label || c.label === label),
  );

  const open = (change: ChangeItem) => {
    const id = change.ids?.[0];
    if (change.kind !== "node" || change.op === "delete" || !id || !change.label) return;
    workspace.open({ id, label: change.label }, { id });
    navigate("/explore");
  };

  return (
    <div className={classes.root}>
      <Group justify="space-between" mb="md">
        <Badge variant="dot" color={status === "live" ? "accent" : "gray"} size="lg" radius="sm" tt="none">
          {status === "live" ? "Live" : status === "retrying" ? "Reconnecting" : "Connecting"}
        </Badge>
        <Group gap="xs">
          <Select
            size="xs"
            placeholder="Everyone"
            data={people}
            value={person}
            onChange={setPerson}
            clearable
            w={160}
            aria-label="Filter by person"
          />
          <Select
            size="xs"
            placeholder="All labels"
            data={labels}
            value={label}
            onChange={setLabel}
            clearable
            w={160}
            aria-label="Filter by label"
          />
        </Group>
      </Group>

      {shown.length === 0 ? (
        <div className={classes.empty}>
          <Text fw={600}>Nothing yet</Text>
          <Text size="sm" c="dimmed">
            Changes made from now on appear here as they happen.
          </Text>
        </div>
      ) : (
        <div className={classes.group}>
          {shown.map((change) => (
            <UnstyledButton key={change.cursor} className={classes.row} onClick={() => open(change)}>
              <Avatar size={28} radius="xl" color="gray">
                {(change.consumer ?? "?").slice(0, 2).toUpperCase()}
              </Avatar>
              <div className={classes.text}>
                <Text size="sm" truncate>
                  <Text span fw={600}>
                    {change.consumer ?? "unknown"}
                  </Text>{" "}
                  {describeChange(change).replace(/^./, (c) => c.toLowerCase())}
                </Text>
                <Text size="xs" c="dimmed">
                  {relativeTime(change.at)}
                </Text>
              </div>
            </UnstyledButton>
          ))}
        </div>
      )}
    </div>
  );
}

function unique(values: string[]): string[] {
  return [...new Set(values)].sort();
}
