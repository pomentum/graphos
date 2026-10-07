import { Button, Group, Select, TextInput } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { FILTER_OPS, needsValue, type BrowseFilter, type FilterOp } from "./browseState";

export interface FilterBarProps {
  /** Property names to filter on. */
  properties: string[];
  /** The filter applied now; the bar starts from it. */
  filter: BrowseFilter | null;
  onApply: (filter: BrowseFilter | null) => void;
}

/**
 * One condition on a property: "name is checkout", "owner is set". Edited as
 * a draft and applied with Enter or Apply, so typing does not run a scan per
 * key. Remount it (key) to start again from a new applied filter.
 */
export function FilterBar({ properties, filter, onApply }: FilterBarProps) {
  const { t } = useTranslation();
  const [property, setProperty] = useState<string | null>(filter?.property ?? null);
  const [op, setOp] = useState<FilterOp>(filter?.op ?? "=");
  const [value, setValue] = useState(filter?.value ?? "");

  const apply = () => {
    if (property) onApply({ property, op, value: needsValue(op) ? value : "" });
  };

  return (
    <Group gap={6} wrap="nowrap">
      <Select
        size="xs"
        w={160}
        placeholder={t("views.browse.filter.propertyPlaceholder")}
        aria-label={t("views.browse.filter.property")}
        data={properties}
        value={property}
        onChange={setProperty}
        searchable
        selectFirstOptionOnChange
        clearable
      />
      <Select
        size="xs"
        w={110}
        aria-label={t("views.browse.filter.condition")}
        data={FILTER_OPS.map((option) => ({ value: option.value, label: t(option.label) }))}
        value={op}
        onChange={(next) => setOp((next as FilterOp | null) ?? "=")}
        allowDeselect={false}
        disabled={!property}
      />
      {needsValue(op) ? (
        <TextInput
          size="xs"
          w={160}
          placeholder={t("views.browse.filter.valuePlaceholder")}
          aria-label={t("views.browse.filter.value")}
          value={value}
          disabled={!property}
          onChange={(event) => setValue(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") apply();
          }}
        />
      ) : null}
      <Button size="xs" variant="default" disabled={!property} onClick={apply}>
        {t("common.actions.apply")}
      </Button>
      {filter ? (
        <Button size="xs" variant="subtle" color="gray" onClick={() => onApply(null)}>
          {t("common.actions.clear")}
        </Button>
      ) : null}
    </Group>
  );
}
