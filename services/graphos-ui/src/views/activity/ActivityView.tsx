import { Avatar, Badge, Button, Group, Select, Text, UnstyledButton } from "@mantine/core";
import { IconChevronRight } from "@tabler/icons-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { ChangeItem } from "../../api/types";
import { useActivity } from "../../data/ActivityProvider";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";
import { describeChange, openableNode, streamStatusText } from "../../lib/changes";
import { relativeTime } from "../../lib/time";
import classes from "./ActivityView.module.css";

/**
 * Activity: every change to the tenant since this page was opened, live,
 * with who made it. A change to one node that still exists opens it in
 * Explore (those rows show a chevron); the others are plain rows.
 */
export function ActivityView() {
  const { t } = useTranslation();
  const unknown = t("common.unknownConsumer");
  const { changes, status, markSeen } = useActivity();
  const { api } = useTenantSession();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [person, setPerson] = useState<string | null>(null);
  const [label, setLabel] = useState<string | null>(null);

  // Everything shown here is seen, including changes that arrive while watching.
  const newest = changes[0]?.cursor;
  useEffect(markSeen, [markSeen, newest]);

  const people = useMemo(() => unique(changes.map((c) => c.consumer ?? unknown)), [changes, unknown]);
  const labels = useMemo(() => unique(changes.map((c) => c.label ?? "")).filter(Boolean), [changes]);
  const shown = changes.filter((c) => (!person || (c.consumer ?? unknown) === person) && (!label || c.label === label));

  /** Opens the node, then fills in its properties (the change carries only its id). */
  const open = (change: ChangeItem) => {
    const ref = openableNode(change);
    if (!ref) return;
    const key = workspace.open(ref, { id: ref.id });
    void api
      .node(ref.id, ref.label)
      .then((record) => workspace.model.updateNode(key, record.properties))
      .catch(() => undefined);
    void navigate("/explore");
  };
  const filtered = Boolean(person || label);

  return (
    <div className={classes.root}>
      <Group justify="space-between" mb="md">
        <Badge variant="dot" color={status === "live" ? "accent" : "gray"} size="lg" radius="sm" tt="none">
          {streamStatusText(status)}
        </Badge>
        <Group gap="xs">
          <Select
            size="xs"
            placeholder={t("views.activity.everyone")}
            data={people}
            value={person}
            onChange={setPerson}
            clearable
            w={160}
            aria-label={t("views.activity.byPerson")}
          />
          <Select
            size="xs"
            placeholder={t("views.activity.allLabels")}
            data={labels}
            value={label}
            onChange={setLabel}
            clearable
            w={160}
            aria-label={t("views.activity.byLabel")}
          />
        </Group>
      </Group>

      {shown.length === 0 ? (
        <div className={classes.empty}>
          <Text fw={600}>{filtered ? t("views.activity.noMatch") : t("views.activity.empty")}</Text>
          <Text size="sm" c="dimmed">
            {filtered ? t("views.activity.hidden", { count: changes.length }) : t("views.activity.emptyHint")}
          </Text>
          {filtered ? (
            <Button
              size="xs"
              variant="default"
              mt="sm"
              onClick={() => {
                setPerson(null);
                setLabel(null);
              }}
            >
              {t("views.activity.clearFilters")}
            </Button>
          ) : null}
        </div>
      ) : (
        <div className={classes.group}>
          {shown.map((change) => {
            const openable = openableNode(change) !== null;
            const Row = openable ? UnstyledButton : "div";
            return (
              <Row
                key={change.cursor}
                className={classes.row}
                data-openable={openable || undefined}
                onClick={openable ? () => open(change) : undefined}
                title={openable ? t("common.actions.openInExplore") : undefined}
              >
                <Avatar size={28} radius="xl" color="gray">
                  {(change.consumer ?? "?").slice(0, 2).toUpperCase()}
                </Avatar>
                <div className={classes.text}>
                  <Text size="sm" truncate title={describeChange(change)}>
                    <Text span fw={600}>
                      {change.consumer ?? unknown}
                    </Text>{" "}
                    {describeChange(change).replace(/^./, (c) => c.toLowerCase())}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {relativeTime(change.at)}
                  </Text>
                </div>
                {openable ? <IconChevronRight size={14} className="gx-muted" /> : null}
              </Row>
            );
          })}
        </div>
      )}
    </div>
  );
}

function unique(values: string[]): string[] {
  return [...new Set(values)].sort();
}
