import { Button, CloseButton, Group, Loader, Select, Stack, Text } from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { GraphCanvas } from "../../components/GraphCanvas";
import { useCommunityMap } from "../../data/queries";
import { buildCommunityModel, communityCategories, groupKey, type Community } from "../../graph/communityGraph";
import { largestBubbles, mapEdgeDisplay, mapNodeDisplay } from "../../graph/graphMap";
import { useWorkspace, type ExploreMode } from "../../graph/WorkspaceProvider";
import { relativeTime } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import { usePalette } from "../../theme/usePalette";
import classes from "./ExploreView.module.css";
import { GroupPanel } from "./GroupPanel";
import { ModeSwitch } from "./ModeSwitch";
import { useRecomputeGroups } from "./useRecomputeGroups";

const GROUP_STYLE = { node: mapNodeDisplay, edge: mapEdgeDisplay };
/** How many of the largest groups are drawn; more crowd the canvas without saying more. */
const SHOWN_CHOICES = [25, 50, 100, 150];
const DEFAULT_SHOWN = 50;
const NO_FOCUS = { nodes: new Set<string>(), edges: new Set<string>() };

/**
 * The graph by its groups (Louvain communities): one bubble per group, sized
 * by its members and named after its best-connected member, and a line per
 * pair of groups with relationships between them, thicker with more. Click
 * a group to see what it is made of and what it connects to; double-click
 * to open its best-connected member on the graph. The groups are those of
 * the last recompute; whoever maintains the tenant can compute them again.
 */
export function GroupsView({ onMode }: { onMode: (mode: ExploreMode) => void }) {
  const { t } = useTranslation();
  const { canMaintain } = useTenantSession();
  const workspace = useWorkspace();
  const map = useCommunityMap();
  const palette = usePalette();
  // Kept in the workspace: leaving the view keeps the group selected.
  const { selectedGroup: selected, setSelectedGroup: setSelected } = workspace;
  const { recompute, running } = useRecomputeGroups();

  const data = map.data?.available ? map.data : null;
  const [shown, setShown] = useState(DEFAULT_SHOWN);
  const model = useMemo(() => (data ? buildCommunityModel(data, shown) : null), [data, shown]);
  const largest = useMemo(() => (model ? largestBubbles(model) : undefined), [model]);
  const categories = useMemo(() => (data ? communityCategories(data) : undefined), [data]);
  const emphasis = useMemo(
    () => ({
      selected,
      categories,
      focus: selected && model?.graph.hasNode(selected) ? model.neighborhood(selected) : NO_FOCUS,
      recent: new Set<string>(),
      hubs: largest,
    }),
    [selected, categories, model, largest],
  );
  const byKey = useMemo(() => new Map((data?.communities ?? []).map((c) => [groupKey(c.community_id), c])), [data]);
  const community = selected ? byKey.get(selected) : undefined;

  /** Puts the group's best-connected member on the graph and shows it. */
  const openHub = (group: Community) => {
    if (!group.hub) return;
    workspace.open({ id: group.hub.id, label: group.hub.label }, group.hub.properties);
  };

  const computeButton = canMaintain ? (
    <Button
      size="xs"
      variant="default"
      leftSection={<IconRefresh size={14} />}
      loading={running}
      onClick={() => void recompute()}
    >
      {data ? t("views.explore.groups.recompute") : t("views.explore.groups.compute")}
    </Button>
  ) : null;

  return (
    <div className={classes.root} data-inspector={community ? "open" : "closed"}>
      <section className={classes.canvas} aria-label={t("views.explore.groups.region")}>
        {model ? (
          <GraphCanvas
            model={model}
            emphasis={emphasis}
            palette={palette}
            style={GROUP_STYLE}
            onSelect={setSelected}
            onExpand={(key) => {
              const group = byKey.get(key);
              if (group) openHub(group);
            }}
          />
        ) : (
          <div className={classes.empty}>
            {map.isLoading ? (
              <>
                <Loader size="sm" />
                <Text size="sm" c="dimmed">
                  {t("views.explore.groups.loading")}
                </Text>
              </>
            ) : map.error ? (
              <Text size="sm" c="red">
                {map.error.message}
              </Text>
            ) : (
              <Stack gap={6} align="center" maw={440} className={classes.emptyAction}>
                <Text fw={600}>{t("views.explore.groups.notComputedTitle")}</Text>
                <Text size="sm" c="dimmed">
                  {t("views.explore.groups.notComputedBody")}
                </Text>
                {running ? (
                  <Text size="sm" c="dimmed">
                    {t("views.explore.groups.computing")}
                  </Text>
                ) : null}
                {computeButton ?? (
                  <Text size="sm" c="dimmed">
                    {t("views.explore.groups.askAdmin")}
                  </Text>
                )}
              </Stack>
            )}
          </div>
        )}
        <Group gap={6} className={classes.actions}>
          <ModeSwitch mode="groups" onChange={onMode} />
          {data ? (
            <Select
              size="xs"
              w={150}
              aria-label={t("views.explore.groups.shownLabel")}
              data={SHOWN_CHOICES.filter((n) => n <= Math.max(data.communities.length, SHOWN_CHOICES[0]!)).map((n) => ({
                value: String(n),
                label: t("views.explore.groups.shownOption", { count: n }),
              }))}
              value={String(shown)}
              onChange={(value) => value && setShown(Number(value))}
              allowDeselect={false}
            />
          ) : null}
          {data ? computeButton : null}
        </Group>
        {data ? (
          <Text size="xs" c="dimmed" className={classes.note} title={t("views.explore.groups.hint")}>
            {running
              ? t("views.explore.groups.computing")
              : t("views.explore.groups.totals", {
                  shown: Math.min(shown, data.communities.length),
                  total: data.n_communities,
                  when: data.computed_at ? relativeTime(data.computed_at) : "",
                })}
          </Text>
        ) : null}
      </section>

      {community && data ? (
        <aside className={classes.inspector} aria-label={t("views.explore.groups.panel")}>
          <CloseButton
            className={classes.close}
            aria-label={t("common.actions.close")}
            onClick={() => setSelected(null)}
          />
          <GroupPanel
            map={data}
            community={community}
            onOpenHub={openHub}
            onSelectGroup={(id) => setSelected(groupKey(id))}
          />
        </aside>
      ) : null}
    </div>
  );
}
