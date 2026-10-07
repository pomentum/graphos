import { Badge, Button, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconTable, IconTargetArrow } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import type { CommunityMap } from "../../api/types";
import { CountBars } from "../../components/CountBars";
import { groupLinks, groupTitle, type Community } from "../../graph/communityGraph";
import { formatCount } from "../../lib/time";
import { browseParams } from "../browse/browseState";
import classes from "./MapPanel.module.css";

/** Links listed per direction; the rest are summarised. */
const SHOWN_LINKS = 12;

export interface GroupPanelProps {
  map: CommunityMap;
  community: Community;
  onOpenHub: (community: Community) => void;
  onSelectGroup: (id: number) => void;
}

/**
 * The selected group: its size, what it is made of (members per label),
 * its best-connected member, how it connects to other groups, and the ways
 * in: open the hub on the graph, or browse the members of its main label.
 */
export function GroupPanel({ map, community, onOpenHub, onSelectGroup }: GroupPanelProps) {
  const { t } = useTranslation();
  const { outgoing, incoming } = groupLinks(map, community.community_id);
  const byId = new Map(map.communities.map((c) => [c.community_id, c]));
  const mainLabel = Object.keys(community.labels)[0];
  const browse = mainLabel
    ? `/browse?${browseParams({
        label: mainLabel,
        filter: { property: "community_id", op: "=", value: String(community.community_id) },
      }).toString()}`
    : null;

  const list = (title: string, rows: { id: number; count: number }[]) =>
    rows.length === 0 ? null : (
      <div>
        <Text size="xs" c="dimmed" mb={6}>
          {title}
        </Text>
        <div className={classes.group}>
          {rows.slice(0, SHOWN_LINKS).map((row) => {
            const other = byId.get(row.id);
            const name = other ? groupTitle(other) : t("views.explore.groups.fallbackName", { id: row.id });
            return (
              <UnstyledButton key={row.id} className={classes.row} onClick={() => onSelectGroup(row.id)}>
                <Text size="sm" truncate className={classes.label} title={name}>
                  {name}
                </Text>
                <Text size="xs" c="dimmed">
                  {formatCount(row.count)}
                </Text>
              </UnstyledButton>
            );
          })}
          {rows.length > SHOWN_LINKS ? (
            <Text size="xs" c="dimmed" className={classes.row}>
              {t("views.explore.groups.more", { count: rows.length - SHOWN_LINKS })}
            </Text>
          ) : null}
        </div>
      </div>
    );

  return (
    <Stack gap="md" className={classes.root}>
      <div>
        <Text fw={600} size="md" truncate title={groupTitle(community)}>
          {groupTitle(community)}
        </Text>
        <Group gap={6} mt={4}>
          <Badge variant="light" size="sm" radius="sm" tt="none">
            {t("views.explore.groups.members", { count: community.size, formatted: formatCount(community.size) })}
          </Badge>
          <Text size="xs" c="dimmed">
            {t("views.explore.groups.internal", {
              count: community.internal_edges,
              formatted: formatCount(community.internal_edges),
            })}
          </Text>
        </Group>
        <Stack gap={6} mt="sm">
          {community.hub ? (
            <Button size="xs" leftSection={<IconTargetArrow size={14} />} onClick={() => onOpenHub(community)}>
              {t("views.explore.groups.openHub")}
            </Button>
          ) : null}
          {browse && mainLabel ? (
            <Button size="xs" variant="default" leftSection={<IconTable size={14} />} component={Link} to={browse}>
              {t("views.explore.groups.browseMembers", { label: mainLabel })}
            </Button>
          ) : null}
        </Stack>
      </div>

      <div>
        <Text size="xs" c="dimmed" mb={6}>
          {t("views.explore.groups.makeup")}
        </Text>
        <CountBars rows={Object.entries(community.labels).map(([name, count]) => ({ name, count }))} />
      </div>

      {community.hub ? (
        <div>
          <Text size="xs" c="dimmed" mb={6}>
            {t("views.explore.groups.hub")}
          </Text>
          <Text size="sm" truncate title={community.hub.id}>
            {groupTitle(community)}{" "}
            <Text span size="xs" c="dimmed">
              {community.hub.label} ·{" "}
              {t("views.explore.groups.hubDegree", {
                count: community.hub.degree,
                formatted: formatCount(community.hub.degree),
              })}
            </Text>
          </Text>
        </div>
      ) : null}

      {list(t("views.explore.groups.linksTo"), outgoing)}
      {list(t("views.explore.groups.linksFrom"), incoming)}
    </Stack>
  );
}
