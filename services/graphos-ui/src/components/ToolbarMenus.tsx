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
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { useActivity } from "../data/ActivityProvider";
import { describeChange, streamStatusText } from "../lib/changes";
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
  const { t } = useTranslation();
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
  const shown = needle ? tenants.filter((tenant) => tenant.toLowerCase().includes(needle)) : tenants;

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
          aria-label={t("shell.tenants.switch")}
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
            placeholder={t("shell.tenants.findAmong", { count: tenants.length })}
            aria-label={t("shell.tenants.find")}
          />
        ) : null}
        <Combobox.Options>
          <ScrollArea.Autosize mah="min(360px, calc(100vh - 140px))" type="auto">
            {shown.map((tenant) => (
              <Combobox.Option key={tenant} value={tenant} active={tenant === session.tenant}>
                <Group justify="space-between" wrap="nowrap" gap="xs">
                  <Text size="sm" truncate title={tenant}>
                    {tenant}
                  </Text>
                  {tenant === session.tenant ? <IconCheck size={14} /> : null}
                </Group>
              </Combobox.Option>
            ))}
            {shown.length === 0 ? <Combobox.Empty>{t("shell.tenants.noMatch")}</Combobox.Empty> : null}
          </ScrollArea.Autosize>
        </Combobox.Options>
      </Combobox.Dropdown>
    </Combobox>
  );
}

/** The bell: a dot when others changed something, and the latest changes on click. */
export function ActivityBell() {
  const { t } = useTranslation();
  const { changes, unseen, markSeen, status } = useActivity();
  return (
    <Popover position="bottom-end" width={300} onOpen={markSeen} radius="lg">
      <Popover.Target>
        <Indicator disabled={unseen === 0} size={7} offset={4} color="accent">
          <ActionIcon
            variant="subtle"
            color="gray"
            aria-label={unseen ? t("shell.activityBell.labelUnseen", { unseen }) : t("shell.activityBell.label")}
          >
            <IconBell size={17} stroke={1.7} />
          </ActionIcon>
        </Indicator>
      </Popover.Target>
      <Popover.Dropdown>
        <Text size="xs" c="dimmed" mb={6}>
          {streamStatusText(status)}
        </Text>
        <Stack gap={8}>
          {changes.slice(0, 5).map((change) => (
            <div key={change.cursor}>
              <Text size="sm" lineClamp={1}>
                {describeChange(change)}
              </Text>
              <Text size="xs" c="dimmed">
                {t("shell.activityBell.byWhen", {
                  who: change.consumer ?? t("common.unknownConsumer"),
                  when: relativeTime(change.at),
                })}
              </Text>
            </div>
          ))}
          {changes.length === 0 ? (
            <Text size="sm" c="dimmed">
              {t("shell.activityBell.empty")}
            </Text>
          ) : null}
        </Stack>
        <Text component={Link} to="/activity" size="xs" c="var(--gx-accent-text)" mt="sm" display="block">
          {t("shell.activityBell.showAll")}
        </Text>
      </Popover.Dropdown>
    </Popover>
  );
}

/** Who is signed in, the colour scheme, and sign out. */
export function AccountMenu() {
  const { t } = useTranslation();
  const { me, canWrite } = useTenantSession();
  const { signOut } = useSessionContext();
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const schemes = [
    { value: "auto", label: t("shell.account.schemes.system"), icon: IconSunMoon },
    { value: "light", label: t("shell.account.schemes.light"), icon: IconSun },
    { value: "dark", label: t("shell.account.schemes.dark"), icon: IconMoon },
  ] as const;
  return (
    <Menu position="bottom-end" width={210}>
      <Menu.Target>
        <Avatar
          component="button"
          size={26}
          radius="xl"
          color="gray"
          aria-label={t("shell.account.label")}
          style={{ cursor: "pointer", border: 0 }}
        >
          {me.consumer.slice(0, 2).toUpperCase()}
        </Avatar>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>
          {t("shell.account.whoAndAccess", {
            consumer: me.consumer,
            access: canWrite ? t("shell.account.canEdit") : t("shell.account.readOnly"),
          })}
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
          {t("shell.account.signOut")}
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}
