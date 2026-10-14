import type { Icon } from "@tabler/icons-react";
import {
  IconBook,
  IconBox,
  IconBug,
  IconBuilding,
  IconCalendar,
  IconChartBar,
  IconCloud,
  IconCode,
  IconCpu,
  IconDatabase,
  IconFileText,
  IconFlag,
  IconFolder,
  IconGitBranch,
  IconKey,
  IconLink,
  IconLock,
  IconMail,
  IconMapPin,
  IconMathFunction,
  IconMessage,
  IconPackage,
  IconServer,
  IconSettings,
  IconShield,
  IconStar,
  IconTag,
  IconUser,
  IconUsers,
  IconWorld,
} from "@tabler/icons-react";
import { __iconNode as outlineUser } from "@tabler/icons-react/dist/esm/icons/IconUser.mjs";
import { __iconNode as outlineUsers } from "@tabler/icons-react/dist/esm/icons/IconUsers.mjs";
import { __iconNode as outlineBuilding } from "@tabler/icons-react/dist/esm/icons/IconBuilding.mjs";
import { __iconNode as outlineFileText } from "@tabler/icons-react/dist/esm/icons/IconFileText.mjs";
import { __iconNode as outlineFolder } from "@tabler/icons-react/dist/esm/icons/IconFolder.mjs";
import { __iconNode as outlineCode } from "@tabler/icons-react/dist/esm/icons/IconCode.mjs";
import { __iconNode as outlineMathFunction } from "@tabler/icons-react/dist/esm/icons/IconMathFunction.mjs";
import { __iconNode as outlineDatabase } from "@tabler/icons-react/dist/esm/icons/IconDatabase.mjs";
import { __iconNode as outlineServer } from "@tabler/icons-react/dist/esm/icons/IconServer.mjs";
import { __iconNode as outlineCloud } from "@tabler/icons-react/dist/esm/icons/IconCloud.mjs";
import { __iconNode as outlinePackage } from "@tabler/icons-react/dist/esm/icons/IconPackage.mjs";
import { __iconNode as outlineTag } from "@tabler/icons-react/dist/esm/icons/IconTag.mjs";
import { __iconNode as outlineLink } from "@tabler/icons-react/dist/esm/icons/IconLink.mjs";
import { __iconNode as outlineCalendar } from "@tabler/icons-react/dist/esm/icons/IconCalendar.mjs";
import { __iconNode as outlineMapPin } from "@tabler/icons-react/dist/esm/icons/IconMapPin.mjs";
import { __iconNode as outlineMail } from "@tabler/icons-react/dist/esm/icons/IconMail.mjs";
import { __iconNode as outlineKey } from "@tabler/icons-react/dist/esm/icons/IconKey.mjs";
import { __iconNode as outlineLock } from "@tabler/icons-react/dist/esm/icons/IconLock.mjs";
import { __iconNode as outlineShield } from "@tabler/icons-react/dist/esm/icons/IconShield.mjs";
import { __iconNode as outlineBug } from "@tabler/icons-react/dist/esm/icons/IconBug.mjs";
import { __iconNode as outlineStar } from "@tabler/icons-react/dist/esm/icons/IconStar.mjs";
import { __iconNode as outlineFlag } from "@tabler/icons-react/dist/esm/icons/IconFlag.mjs";
import { __iconNode as outlineChartBar } from "@tabler/icons-react/dist/esm/icons/IconChartBar.mjs";
import { __iconNode as outlineSettings } from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import { __iconNode as outlineWorld } from "@tabler/icons-react/dist/esm/icons/IconWorld.mjs";
import { __iconNode as outlineBook } from "@tabler/icons-react/dist/esm/icons/IconBook.mjs";
import { __iconNode as outlineCpu } from "@tabler/icons-react/dist/esm/icons/IconCpu.mjs";
import { __iconNode as outlineGitBranch } from "@tabler/icons-react/dist/esm/icons/IconGitBranch.mjs";
import { __iconNode as outlineMessage } from "@tabler/icons-react/dist/esm/icons/IconMessage.mjs";
import { __iconNode as outlineBox } from "@tabler/icons-react/dist/esm/icons/IconBox.mjs";
import type { DisplayIcon } from "../api/types";

/** The pictogram of each icon a label may declare, as a React component (legend, tables, the schema page). */
export const LABEL_ICONS: Record<DisplayIcon, Icon> = {
  person: IconUser,
  people: IconUsers,
  building: IconBuilding,
  document: IconFileText,
  folder: IconFolder,
  code: IconCode,
  function: IconMathFunction,
  database: IconDatabase,
  server: IconServer,
  cloud: IconCloud,
  package: IconPackage,
  tag: IconTag,
  link: IconLink,
  calendar: IconCalendar,
  location: IconMapPin,
  mail: IconMail,
  key: IconKey,
  lock: IconLock,
  shield: IconShield,
  bug: IconBug,
  star: IconStar,
  flag: IconFlag,
  chart: IconChartBar,
  settings: IconSettings,
  globe: IconWorld,
  book: IconBook,
  cpu: IconCpu,
  branch: IconGitBranch,
  message: IconMessage,
  box: IconBox,
};

type IconNode = [tag: string, attributes: Record<string, string>][];

const NODES: Record<DisplayIcon, IconNode> = {
  person: outlineUser,
  people: outlineUsers,
  building: outlineBuilding,
  document: outlineFileText,
  folder: outlineFolder,
  code: outlineCode,
  function: outlineMathFunction,
  database: outlineDatabase,
  server: outlineServer,
  cloud: outlineCloud,
  package: outlinePackage,
  tag: outlineTag,
  link: outlineLink,
  calendar: outlineCalendar,
  location: outlineMapPin,
  mail: outlineMail,
  key: outlineKey,
  lock: outlineLock,
  shield: outlineShield,
  bug: outlineBug,
  star: outlineStar,
  flag: outlineFlag,
  chart: outlineChartBar,
  settings: outlineSettings,
  globe: outlineWorld,
  book: outlineBook,
  cpu: outlineCpu,
  branch: outlineGitBranch,
  message: outlineMessage,
  box: outlineBox,
};

const images = new Map<DisplayIcon, string>();

/**
 * The icon as an image for the canvas: Tabler's outline drawn in white on
 * nothing, as an SVG data URL (sigma's pictogram program tints it).
 */
export function iconImage(icon: DisplayIcon): string {
  let url = images.get(icon);
  if (!url) {
    const parts = NODES[icon]
      .map(([tag, attributes]) => {
        const attrs = Object.entries(attributes)
          .filter(([name]) => name !== "key")
          .map(([name, value]) => `${name}="${value}"`)
          .join(" ");
        return `<${tag} ${attrs}/>`;
      })
      .join("");
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24" fill="none" ' +
      `stroke="#FFFFFF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${parts}</svg>`;
    url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    images.set(icon, url);
  }
  return url;
}
