import {
  ActionIcon,
  Avatar,
  Indicator,
  Menu,
  Popover,
  Stack,
  Text,
  UnstyledButton,
  useMantineColorScheme,
} from "@mantine/core";
import { IconBell, IconCheck, IconChevronDown, IconLogout, IconMoon, IconSun, IconSunMoon } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import { useActivity } from "../data/ActivityProvider";
import { describeChange } from "../lib/changes";
import { relativeTime } from "../lib/time";
import { useSessionContext, useTenantSession } from "../session/SessionProvider";

/** The tenant name with a menu of the other tenants the token may use. */
export function TenantSwitcher() {
  const { session, selectTenant } = useSessionContext();
  if (!session?.tenant) return null;
  const others = session.me.tenants;
  return (
    <Menu position="bottom-start" width={200} disabled={others.length < 2}>
      <Menu.Target>
        <UnstyledButton aria-label="Switch tenant">
          <Text fw={600} size="sm" span>
            {session.tenant}
          </Text>
          {others.length > 1 ? <IconChevronDown size={12} style={{ marginLeft: 4 }} /> : null}
        </UnstyledButton>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Tenants</Menu.Label>
        {others.map((tenant) => (
          <Menu.Item
            key={tenant}
            onClick={() => selectTenant(tenant)}
            rightSection={tenant === session.tenant ? <IconCheck size={14} /> : null}
          >
            {tenant}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
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
