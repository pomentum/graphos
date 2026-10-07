import {
  Alert,
  Button,
  Group,
  Input,
  MultiSelect,
  SegmentedControl,
  Slider,
  Stack,
  Text,
  UnstyledButton,
} from "@mantine/core";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import type { Direction, GraphPath, SearchHit } from "../../api/types";
import { GraphCanvas } from "../../components/GraphCanvas";
import { NodeSearch } from "../../components/NodeSearch";
import { usePaths, useSchema } from "../../data/queries";
import { displayName, edgeKey, GraphModel, nodeKey } from "../../graph/GraphModel";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { usePalette } from "../../theme/usePalette";
import classes from "./PathView.module.css";
import { PathSummary } from "./PathSummary";
import { MAX_HOPS, pathParams, readPathState, type Endpoint, type PathState } from "./pathState";

const MAX_PATHS = 10;

function endpoint(hit: SearchHit): Endpoint {
  const id = String(hit.properties.id ?? hit.id);
  return { id, label: hit.label, name: displayName(hit.properties, id) };
}

/**
 * Path: how two nodes are connected, as every shortest path between them.
 * The ends and options live in the address, so leaving Path and coming
 * back (or sharing the link) shows the same search, run again.
 */
export function PathView() {
  const palette = usePalette();
  const schema = useSchema();
  const paths = usePaths();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const state = readPathState(params);
  const { from, to, direction, maxHops, relTypes } = state;
  const update = (change: Partial<PathState>) => setParams(pathParams({ ...state, ...change }), { replace: true });
  const [chosen, setChosen] = useState<number | null>(null);
  // The hop limit the shown result was searched with (the slider may have moved since).
  const [searchedHops, setSearchedHops] = useState(maxHops);
  const [model] = useState(() => new GraphModel());
  const [focus, setFocus] = useState({ nodes: new Set<string>(), edges: new Set<string>() });

  const find = async () => {
    if (!from || !to) return;
    const result = await paths.mutateAsync({
      src: from.id,
      src_label: from.label,
      dst: to.id,
      dst_label: to.label,
      rel_types: relTypes.length ? relTypes : undefined,
      direction,
      max_hops: maxHops,
      limit: MAX_PATHS,
      include_properties: true,
    });
    setSearchedHops(maxHops);
    model.clear();
    setChosen(null);
    setFocus(model.addPaths(result.paths));
  };

  const highlight = (index: number | null, all: GraphPath[]) => {
    setChosen(index);
    const shown = index === null ? all : all.slice(index, index + 1);
    setFocus({
      nodes: new Set(shown.flatMap((p) => p.nodes.map((n) => nodeKey(n)))),
      edges: new Set(
        shown.flatMap((p) =>
          p.edges.map((e) => edgeKey(e.type, nodeKey(nodeOf(p, e.src_id)), nodeKey(nodeOf(p, e.dst_id)))),
        ),
      ),
    });
  };

  // Coming back to Path with both ends in the address: show the result again, once.
  const ranOnce = useRef(false);
  useEffect(() => {
    if (ranOnce.current || !from || !to) return;
    ranOnce.current = true;
    void find().catch(() => undefined);
  });

  const openInExplore = (path: GraphPath) => {
    const added = workspace.model.addPaths([path]);
    workspace.setMode("graph");
    workspace.frameOn([...added.nodes]);
    void navigate("/explore");
  };

  const emphasis = useMemo(() => ({ selected: null, focus, recent: new Set<string>() }), [focus]);
  const result = paths.data;

  return (
    <div className={classes.root}>
      <aside className={classes.form} aria-label="Path options">
        <Stack gap="md">
          <Input.Wrapper label="From">
            <NodeSearch
              placeholder="Start node"
              keepSelection
              scoped
              width="100%"
              initialText={from?.name}
              onEdit={() => from && update({ from: null })}
              onPick={(hit) => update({ from: endpoint(hit) })}
            />
          </Input.Wrapper>
          <Input.Wrapper label="To">
            <NodeSearch
              placeholder="End node"
              keepSelection
              scoped
              width="100%"
              initialText={to?.name}
              onEdit={() => to && update({ to: null })}
              onPick={(hit) => update({ to: endpoint(hit) })}
            />
          </Input.Wrapper>
          <Input.Wrapper label="Direction">
            <SegmentedControl
              fullWidth
              size="xs"
              value={direction}
              onChange={(value) => update({ direction: value as Direction })}
              data={[
                { value: "both", label: "Either" },
                { value: "out", label: "Forward" },
                { value: "in", label: "Backward" },
              ]}
            />
          </Input.Wrapper>
          <Input.Wrapper label={`Up to ${maxHops} hops`}>
            <Slider
              min={1}
              max={MAX_HOPS}
              step={1}
              value={maxHops}
              onChange={(hops) => update({ maxHops: hops })}
              label={null}
              mt={6}
            />
          </Input.Wrapper>
          <MultiSelect
            label="Relationships"
            placeholder={relTypes.length ? undefined : "All"}
            data={schema.data?.edge_labels ?? []}
            value={relTypes}
            onChange={(next) => update({ relTypes: next })}
            searchable
            selectFirstOptionOnChange
            clearable
          />
          <Button disabled={!from || !to} loading={paths.isPending} onClick={() => void find()}>
            Find paths
          </Button>
          {paths.error ? (
            <Alert color="red" variant="light" radius="md">
              {paths.error.message}
            </Alert>
          ) : null}
          {result ? <PathSummary result={result} maxHops={searchedHops} /> : null}
          {result?.paths.map((path, index) => (
            <div key={index} className={classes.path} data-active={chosen === index}>
              <UnstyledButton
                className={classes.pathButton}
                onClick={() => highlight(chosen === index ? null : index, result.paths)}
                onDoubleClick={() => openInExplore(path)}
              >
                <Text size="xs" c="dimmed">
                  Path {index + 1} · {path.edges.length} {path.edges.length === 1 ? "hop" : "hops"}
                </Text>
                <Text size="sm" lineClamp={2}>
                  {path.nodes.map((n) => displayName(n.properties ?? {}, n.id)).join(" → ")}
                </Text>
              </UnstyledButton>
              {chosen === index ? (
                <Group justify="flex-end" mt={4}>
                  <Button size="compact-xs" variant="default" onClick={() => openInExplore(path)}>
                    Open in Explore
                  </Button>
                </Group>
              ) : null}
            </div>
          ))}
        </Stack>
      </aside>
      <section className={classes.canvas} aria-label="Paths">
        <GraphCanvas model={model} emphasis={emphasis} palette={palette} onSelect={() => undefined} />
      </section>
    </div>
  );
}

/** The node of `path` with this id (paths name edge ends by id only). */
function nodeOf(path: GraphPath, id: string) {
  return path.nodes.find((n) => n.id === id) ?? { id, label: "" };
}
