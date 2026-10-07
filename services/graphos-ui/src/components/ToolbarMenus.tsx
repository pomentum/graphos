import {
  ActionIcon,
  Avatar,
  Combobox,
  Group,
  Indicator,
  Menu,
  Popover,
  ScrollArea,
  Stack,
  Text,
  UnstyledButton,
  useCombobox,
  useMantineColorScheme,
} from "@mantine/core";
import { useState } from "react";
import { IconBell, IconCheck, IconChevronDown, IconLogout, IconMoon, IconSun, IconSunMoon } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import { useActivity } from "../data/ActivityProvider";
import { describeChange } from "../lib/changes";
import { relativeTime } from "../lib/time";
import { useSessionContext, useTenantSession } from "../session/SessionProvider";

/** Above this many tenants the switcher offers a search box. */
const TENANT_SEARCH_FROM = 8;

/**
 * The tenant name, opening a list of the tenants the token may use. The
 * list scrolls within the window and can be searched, so a token with
 * hundreds of tenants stays usable; arrows and Enter pick one.
 */
export function TenantSwitcher() {
  const { session, selectTenant } = useSessionContext();
  const [query, setQuery] = useState("");
  const combobox = useCombobox({
    onDropdownOpen: () => combobox.focusSearchInput(),
    onDropdownClose: () => {
      combobox.resetSelectedOption();
      setQuery("");
    },
  });
  if (!session?.tenant) return null;
  const tenants = session.me.tenants;
  const needle = query.trim().toLowerCase();
  const shown = needle ? tenants.filter((t) => t.toLowerCase().includes(needle)) : tenants;

  return (
    <Combobox
      store={combobox}
      width={240}
      position="bottom-start"
      onOptionSubmit={(tenant) => {
        selectTenant(tenant);
        combobox.closeDropdown();
      }}
    >
      <Combobox.Target withAriaAttributes={false}>
        <UnstyledButton
          aria-label="Switch tenant"
          disabled={tenants.length < 2}
          onClick={() => combobox.toggleDropdown()}
        >
          <Text fw={600} size="sm" span>
            {session.tenant}
          </Text>
          {tenants.length > 1 ? <IconChevronDown size={12} style={{ marginLeft: 4 }} /> : null}
        </UnstyledButton>
      </Combobox.Target>
      <Combobox.Dropdown>
        {tenants.length > TENANT_SEARCH_FROM ? (
          <Combobox.Search
            value={query}
            onChange={(event) => {
              setQuery(event.currentTarget.value);
              combobox.updateSelectedOptionIndex();
            }}
            placeholder={`Find among ${tenants.length} tenants`}
            aria-label="Find a tenant"
          />
        ) : null}
        <Combobox.Options>
          <ScrollArea.Autosize mah="min(360px, calc(100vh - 140px))" type="auto">
            {shown.map((tenant) => (
              <Combobox.Option key={tenant} value={tenant} active={tenant === session.tenant}>
                <Group justify="space-between" wrap="nowrap" gap="xs">
                  <Text size="sm" truncate>
                    {tenant}
                  </Text>
                  {tenant === session.tenant ? <IconCheck size={14} /> : null}
                </Group>
              </Combobox.Option>
            ))}
            {shown.length === 0 ? <Combobox.Empty>No tenant matches</Combobox.Empty> : null}
          </ScrollArea.Autosize>
        </Combobox.Options>
      </Combobox.Dropdown>
    </Combobox>
  );
}

/** The bell: a dot when others changed something, and the latest changes on click. */
export function ActivityBell() {
  const { changes, unseen, markSeen, status } = useActivity();
  return (
    <Popover position="bottom-end" width={300} onOpen={markSeen} radius="lg">
      <Popover.Target>
        <Indicator disabled={unseen === 0} size={7} offset={4} color="accent">
          <ActionIcon variant="subtle" color="gray" aria-label={`Activity${unseen ? `, ${unseen} new` : ""}`}>
            <IconBell size={17} stroke={1.7} />
          </ActionIcon>
        </Indicator>
      </Popover.Target>
      <Popover.Dropdown>
        <Text size="xs" c="dimmed" mb={6}>
          {status === "live" ? "Live" : "Reconnecting…"}
        </Text>
        <Stack gap={8}>
          {changes.slice(0, 5).map((change) => (
            <div key={change.cursor}>
              <Text size="sm" lineClamp={1}>
                {describeChange(change)}
              </Text>
              <Text size="xs" c="dimmed">
                {change.consumer ?? "unknown"} · {relativeTime(change.at)}
              </Text>
            </div>
          ))}
          {changes.length === 0 ? (
            <Text size="sm" c="dimmed">
              No changes since you opened graphos.
            </Text>
          ) : null}
        </Stack>
        <Text component={Link} to="/activity" size="xs" c="var(--gx-accent-text)" mt="sm" display="block">
          Show all activity
        </Text>
      </Popover.Dropdown>
    </Popover>
  );
}

/** Who is signed in, the colour scheme, and sign out. */
export function AccountMenu() {
  const { me, canWrite } = useTenantSession();
  const { signOut } = useSessionContext();
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const schemes = [
    { value: "auto", label: "System", icon: IconSunMoon },
    { value: "light", label: "Light", icon: IconSun },
    { value: "dark", label: "Dark", icon: IconMoon },
  ] as const;
  return (
    <Menu position="bottom-end" width={210}>
      <Menu.Target>
        <Avatar
          component="button"
          size={26}
          radius="xl"
          color="gray"
          aria-label="Account"
          style={{ cursor: "pointer", border: 0 }}
        >
          {me.consumer.slice(0, 2).toUpperCase()}
        </Avatar>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>
          {me.consumer} · {canWrite ? "can edit" : "read only"}
        </Menu.Label>
        <Menu.Divider />
        {schemes.map(({ value, label, icon: Icon }) => (
          <Menu.Item
            key={value}
            leftSection={<Icon size={14} />}
            rightSection={colorScheme === value ? <IconCheck size={14} /> : null}
            onClick={() => setColorScheme(value)}
          >
            {label}
          </Menu.Item>
        ))}
        <Menu.Divider />
        <Menu.Item leftSection={<IconLogout size={14} />} onClick={signOut}>
          Sign out
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}
