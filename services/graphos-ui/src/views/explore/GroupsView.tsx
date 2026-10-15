import { Button, CloseButton, Group, Loader, Select, Stack, Text } from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";
import { useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type Sigma from "sigma";
import { CanvasExportMenu } from "../../components/CanvasExportMenu";
import { GraphCanvas } from "../../components/GraphCanvas";
import { useCommunityMap, useServiceInfo } from "../../data/queries";
import { buildCommunityModel, communityCategories, groupKey, type Community } from "../../graph/communityGraph";
import type { EdgeAttributes, NodeAttributes } from "../../graph/GraphModel";
import { largestBubbles, mapEdgeDisplay, mapNodeDisplay } from "../../graph/graphMap";
import { useWorkspace, type ExploreMode } from "../../graph/WorkspaceProvider";
import type { Lens } from "../../lib/metrics";
import { groupsOffer } from "../../lib/features";
import { relativeTime } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import { usePalette } from "../../theme/usePalette";
import classes from "./ExploreView.module.css";
import { GroupPanel } from "./GroupPanel";
import { LensSwitch } from "./LensSwitch";
import { ModeSwitch } from "./ModeSwitch";
import { useRecomputeGroups } from "./useRecomputeGroups";

const GROUP_STYLE = { node: mapNodeDisplay, edge: mapEdgeDisplay };
/** How many of the largest groups are drawn; more crowd the canvas without saying more. */
const SHOWN_CHOICES = [25, 50, 100, 150];
const DEFAULT_SHOWN = 50;

/** The choices worth offering for this many groups (always at least the smallest). */
function shownChoices(groups: number): number[] {
  return SHOWN_CHOICES.filter((n) => n <= Math.max(groups, SHOWN_CHOICES[0]!));
}
const NO_FOCUS = { nodes: new Set<string>(), edges: new Set<string>() };

/**
 * The graph by its groups (Louvain communities): one bubble per group, sized
 * by its members and named after its best-connected member, and a line per
 * pair of groups with relationships between them, thicker with more. Click
 * a group to see what it is made of and what it connects to; double-click
 * to open its best-connected member on the graph. The groups are those of
 * the last recompute; whoever maintains the tenant can compute them again,
 * where the server has the Louvain extra (`/v1/info`).
 */
export function GroupsView({
  onMode,
  lens,
  onLens,
}: {
  onMode: (mode: ExploreMode) => void;
  lens: Lens;
  onLens: (lens: Lens) => void;
}) {
  const { t } = useTranslation();
  const { canMaintain, tenant } = useTenantSession();
  const renderer = useRef<Sigma<NodeAttributes, EdgeAttributes> | null>(null);
  const workspace = useWorkspace();
  const map = useCommunityMap();
  const palette = usePalette();
  // Kept in the workspace: leaving the view keeps the group selected.
  const { selectedGroup: selected, setSelectedGroup: setSelected } = workspace;
  const { recompute, running } = useRecomputeGroups();
  const offer = groupsOffer(canMaintain, useServiceInfo().data?.features.louvain);

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

  const computeButton =
    offer === "allowed" ? (
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
  // Why there is no button, when there is a reason to give.
  const noCompute =
    offer === "notInstalled"
      ? t("views.explore.groups.notInstalled")
      : offer === "noAccess"
        ? t("views.explore.groups.askAdmin")
        : null;

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
            rendererRef={renderer}
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
                {computeButton ??
                  (noCompute ? (
                    <Text size="sm" c="dimmed">
                      {noCompute}
                    </Text>
                  ) : null)}
              </Stack>
            )}
          </div>
        )}
        <Group gap={6} className={classes.actions}>
          <ModeSwitch mode="groups" onChange={onMode} />
          <LensSwitch lens={lens} onChange={onLens} />
          {data ? (
            <Select
              size="xs"
              w={150}
              aria-label={t("views.explore.groups.shownLabel")}
              data={shownChoices(data.communities.length).map((n) => ({
                value: String(n),
                label: t("views.explore.groups.shownOption", { count: n }),
              }))}
              // With few groups the default is not offered: show the largest choice there is.
              value={String(Math.min(shown, shownChoices(data.communities.length).at(-1)!))}
              onChange={(value) => value && setShown(Number(value))}
              allowDeselect={false}
            />
          ) : null}
          {data ? computeButton : null}
          {model ? (
            <CanvasExportMenu
              renderer={renderer}
              graph={model.graph}
              palette={palette}
              name={[tenant, "groups"]}
              json={() => JSON.stringify(data, null, 2)}
            />
          ) : null}
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
