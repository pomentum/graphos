import { isSchemaManager, managerName } from "../lib/schemaManager";
import { useTenantSession } from "../session/SessionProvider";
import { useSchema } from "./queries";

/** Who manages the tenant's schema, and what that leaves the signed-in user. */
export interface SchemaManagement {
  /** The manager's name (its subject without `user:`), or null when nobody manages the schema. */
  manager: string | null;
  /** The signed-in user is the manager. */
  isManager: boolean;
  /**
   * Someone else manages the schema: its structure (labels, types, indexes,
   * properties, pairs, deprecations, renames, drops, imports) is changed in
   * that program, not here. Styles (`display`) are not affected.
   */
  locked: boolean;
  /** May change the structure here: maintains the tenant, and it is not locked. */
  canEditStructure: boolean;
  /** May rename and drop here: owns the tenant, and it is not locked. */
  canOwnStructure: boolean;
  /** May release the manager: owns the tenant (or is an admin), and is not the manager. */
  canRelease: boolean;
}

/**
 * The tenant's schema manager as it applies to the signed-in user, combined
 * with their level on the tenant (`canMaintain`, `isOwner`). Read from the
 * cached schema, so a refusal that reads it again updates every control.
 */
export function useSchemaManager(): SchemaManagement {
  const { me, canMaintain, isOwner } = useTenantSession();
  const schema = useSchema();
  const subject = schema.data?.schema_manager ?? null;
  const isManager = isSchemaManager(subject, me);
  const locked = subject !== null && !isManager;
  return {
    manager: managerName(subject),
    isManager,
    locked,
    canEditStructure: canMaintain && !locked,
    canOwnStructure: isOwner && !locked,
    canRelease: subject !== null && isOwner && !isManager,
  };
}
