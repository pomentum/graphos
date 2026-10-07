import {
  Alert,
  Button,
  Input,
  MultiSelect,
  SegmentedControl,
  Slider,
  Stack,
  Text,
  UnstyledButton,
} from "@mantine/core";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Direction, GraphPath, SearchHit } from "../../api/types";
import { GraphCanvas } from "../../components/GraphCanvas";
import { NodeSearch } from "../../components/NodeSearch";
import { usePaths, useSchema } from "../../data/queries";
import { edgeKey, GraphModel, nodeKey } from "../../graph/GraphModel";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { usePalette } from "../../theme/usePalette";
import classes from "./PathView.module.css";
import { PathSummary } from "./PathSummary";

interface Endpoint {
  id: string;
  label: string;
}

const MAX_PATHS = 10;

function endpoint(hit: SearchHit): Endpoint {
  return { id: String(hit.properties.id ?? hit.id), label: hit.label };
}

/** Path: how two nodes are connected, as every shortest path between them. */
export function PathView() {
  const palette = usePalette();
  const schema = useSchema();
  const paths = usePaths();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [from, setFrom] = useState<Endpoint | null>(null);
  const [to, setTo] = useState<Endpoint | null>(null);
  const [direction, setDirection] = useState<Direction>("both");
  const [maxHops, setMaxHops] = useState(4);
  const [relTypes, setRelTypes] = useState<string[]>([]);
  const [chosen, setChosen] = useState<number | null>(null);
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

  const openInExplore = (path: GraphPath) => {
    workspace.model.addPaths([path]);
    navigate("/explore");
  };

  const emphasis = useMemo(() => ({ selected: null, focus, recent: new Set<string>() }), [focus]);
  const result = paths.data;

  return (
    <div className={classes.root}>
      <aside className={classes.form} aria-label="Path options">
        <Stack gap="md">
          <Input.Wrapper label="From">
            <NodeSearch placeholder="Start node" keepSelection width="100%" onPick={(hit) => setFrom(endpoint(hit))} />
          </Input.Wrapper>
          <Input.Wrapper label="To">
            <NodeSearch placeholder="End node" keepSelection width="100%" onPick={(hit) => setTo(endpoint(hit))} />
          </Input.Wrapper>
          <Input.Wrapper label="Direction">
            <SegmentedControl
              fullWidth
              size="xs"
              value={direction}
              onChange={(value) => setDirection(value as Direction)}
              data={[
                { value: "both", label: "Either" },
                { value: "out", label: "Forward" },
                { value: "in", label: "Backward" },
              ]}
            />
          </Input.Wrapper>
          <Input.Wrapper label={`Up to ${maxHops} hops`}>
            <Slider min={1} max={8} step={1} value={maxHops} onChange={setMaxHops} label={null} mt={6} />
          </Input.Wrapper>
          <MultiSelect
            label="Relationships"
            placeholder={relTypes.length ? undefined : "All"}
            data={schema.data?.edge_labels ?? []}
            value={relTypes}
            onChange={setRelTypes}
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
          {result ? <PathSummary result={result} maxHops={maxHops} /> : null}
          {result?.paths.map((path, index) => (
            <UnstyledButton
              key={index}
              className={classes.path}
              data-active={chosen === index}
              onClick={() => highlight(chosen === index ? null : index, result.paths)}
              onDoubleClick={() => openInExplore(path)}
            >
              <Text size="xs" c="dimmed">
                Path {index + 1} · double-click to open in Explore
              </Text>
              <Text size="sm" lineClamp={2}>
                {path.nodes.map((n) => String(n.properties?.name ?? n.id)).join(" → ")}
              </Text>
            </UnstyledButton>
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
