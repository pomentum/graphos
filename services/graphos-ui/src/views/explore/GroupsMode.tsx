import { useWorkspace, type ExploreMode } from "../../graph/WorkspaceProvider";
import { ComponentsView } from "./ComponentsView";
import { GroupsView } from "./GroupsView";
import { ScoreView } from "./ScoreView";

/**
 * Explore's Groups mode: what the graph algorithms found, one view at a time.
 * The groups (Louvain communities), the nodes ranked by PageRank,
 * betweenness or degree, or the connected components. The choice is kept in
 * the workspace, so it survives switching modes.
 */
export function GroupsMode({ onMode }: { onMode: (mode: ExploreMode) => void }) {
  const { lens, setLens } = useWorkspace();
  if (lens === "groups") return <GroupsView onMode={onMode} lens={lens} onLens={setLens} />;
  if (lens === "components") return <ComponentsView lens={lens} onLens={setLens} onMode={onMode} />;
  return <ScoreView key={lens} property={lens} lens={lens} onLens={setLens} onMode={onMode} />;
}
